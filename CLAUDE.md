# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Airdrome migrates music libraries and scrobble/play history from cloud services (Spotify, Apple Music, Last.fm, ListenBrainz) to [Navidrome](https://www.navidrome.org/), a self-hosted music server. It ingests metadata, deduplicates tracks, organizes files on disk, and syncs play history.

## Commands

The project interpreter is in `.venv`. Any command that uses packages from `pyproject.toml` must be run either via `uv run <cmd>` or with the virtualenv activated (`.venv/Scripts/activate` on Windows, `.venv/bin/activate` on Unix).

```bash
# Install (requires Python 3.14+)
uv sync

# Start PostgreSQL
docker compose up -d

# Lint / format
ruff check .
ruff format .

# Run tests (requires PostgreSQL running)
uv run pytest

# Run CLI
airdrome --help

# Migrations (schema is auto-applied on CLI startup)
uv run alembic revision --autogenerate -m "<message>"  # after editing models
uv run alembic upgrade head                             # apply pending migrations
uv run alembic downgrade -1                             # roll back one migration
```

## Architecture

### Data flow

1. **Import** — `airdrome import <path>...` auto-detects each source (Apple XML/Media Services, Spotify, Last.fm, ListenBrainz, local music folder) and writes raw `SourceTrack`/`SourcePlaylist`/`TrackFile` and `TrackAlias`/`TrackAliasScrobble` rows. Dumb and per-source; repeat or pass many paths.
2. **Resolve** — `airdrome resolve` builds the canonical graph from *everything imported* (run once, after all imports): unify source tracks/playlists into `Track`/`Playlist` + bind files, then augment, fuzzy-match (`pg_trgm`), and copy scrobbles into `TrackPlay` history. Idempotent; needs the full picture (see `library/unify.py` for the file-binding/playlist-resolution dependencies).
3. **Organize** — move/copy bound files into a structured directory (`library organize`)
4. **Deduplicate** — group duplicate tracks via trigram fuzzy matching (`library deduplicate` / `auto-deduplicate`); duplicates link to a canonical track via `Track.canon_id`. `auto-deduplicate --canon {added,year}` picks which group member becomes canon. Confirmed groups live in `dedupgroup`/`dedupgroupmember`; `library {export,import}-duplicates` round-trip them to a portable JSON file (identity = member-hash multiset) so they survive a DB rebuild
5. **Sync** — push matched play counts + ratings (`navidrome push`) and 3-way-merge playlists (`navidrome playlists`) to Navidrome's SQLite database

### Key models (`airdrome/models.py`)

- **`Track`** — canonical track; normalized title/artist/album fields (`_norm` suffix); self-referential `canon_id` for deduplication
- **`TrackFile`** — audio file on disk with Mutagen-extracted metadata; `is_main` flags the best bitrate copy; `library_path` is the organized destination
- **`TrackAlias`** — a scrobble source entry (one per unique title+artist+album+platform combination); linked to `Track` after matching
- **`TrackAliasScrobble`** — individual play event (timestamp, platform, duration)

### Normalization & matching (`airdrome/normalize/`, `airdrome/match.py`)

All text fields have `_norm` variants: lowercased, accents stripped. Fuzzy deduplication and alias matching use PostgreSQL trigram similarity (`pg_trgm`) with weighted scoring: artist/album_artist at 75%, album at 25%. Threshold defaults to 0.4 and is tunable via CLI flag.

### Cloud connectors (`airdrome/cloud/`)

Each platform has its own `scrobbles.py` (and `ingest.py` for Apple). They parse platform-specific export formats (Spotify JSON, Last.fm CSV, ListenBrainz JSON, iTunes XML) into `TrackAlias`/`TrackAliasScrobble` rows. Spotify plays under 30 seconds are excluded.

### Navidrome integration (`airdrome/navidrome/`)

`navidrome/models.py` maps Navidrome's SQLite schema read-only (`MediaFile`, `Annotation`, `Playlist`, etc.). Sync writes to the `Annotation` table to record play counts and ratings per user.

### CLI (`airdrome/terminal/`)

`app.py` is the Typer root and owns the top-level `import` and `resolve` commands; sub-modules (`library.py`, `navidrome.py`) register their own command groups.

## Configuration

`airdrome/conf.py` defines a Pydantic `Settings` class (via `pydantic-settings`) that loads all config from a `.env` file at the project root. The singleton `settings = Settings()` is imported wherever config is needed.

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `DB_DSN` | `PostgresDsn` | — | PostgreSQL connection string (required) |
| `DB_ECHO` | `bool` | `False` | SQLAlchemy query logging |
| `LIBRARY_DIR` | `Path` | — | Target root for organized music files (required; must be empty on first run) |
| `DUPLICATES_FILEPATH` | `Path` | `data/duplicates.json` | Default file for `library {import,export}-duplicates` (confirmed dedup groups) |
| `NAVIDROME_DB_DSN` | `str \| None` | `None` | Path to Navidrome's SQLite database |

PostgreSQL runs on port **5437** (see `compose.yml`). The `initdb/` directory contains DB initialization scripts.

## Workflow

- For any task beyond a simple bugfix, start a new branch from `main`.
- Cover changes with meaningful tests. Lean on pytest and its fixtures heavily.
- Give every function and method a one-line docstring.
- Comment non-obvious code, explaining the decision made — the more complicated the situation, the more explicit the comment.
- While working, spot and call out places that can be simplified.

## Conventions

- Line length: 110 characters (Ruff)
- Imports: first-party `airdrome` group separated; combined-as imports; two blank lines after imports block
- All datetimes are timezone-aware UTC
- Database operations use `get_or_create()` patterns for idempotency; re-running commands is safe
