# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Airdrome migrates music libraries and scrobble/play history from cloud services (Spotify, Apple Music, Last.fm, ListenBrainz) to [Navidrome](https://www.navidrome.org/), a self-hosted music server. It ingests metadata, deduplicates tracks, organizes files on disk, and syncs play history.

## Commands

```bash
# Install (requires Python 3.14+)
uv sync

# Start PostgreSQL
docker compose up -d

# Lint / format
ruff check .
ruff format .

# Run CLI
airdrome --help
```

No test suite exists yet.

## Architecture

### Data flow

1. **Ingest** — scan local audio files (`library scan`) or import an Apple iTunes XML (`apple import`) → creates `Track` + `TrackFile` records
2. **Organize** — move/copy files into a structured directory (`library organize`)
3. **Deduplicate** — group duplicate tracks via trigram fuzzy matching (`library deduplicate`); duplicates link to a canonical track via `Track.canon_id`
4. **Scrobbles** — import play history from Spotify/Last.fm/ListenBrainz/Apple (`scrobble import`) → `TrackAlias` + `TrackAliasScrobble` records, then fuzzy-match aliases to canonical tracks (`scrobble match`)
5. **Sync** — push matched play counts + ratings + playlists to Navidrome's SQLite database (`navidrome sync-*`)

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

`app.py` is the Typer root; each sub-module (`library.py`, `apple.py`, `scrobble.py`, `navidrome.py`) registers its own command group.

## Configuration

`airdrome/conf.py` defines a Pydantic `Settings` class (via `pydantic-settings`) that loads all config from a `.env` file at the project root. The singleton `settings = Settings()` is imported wherever config is needed.

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `DB_DSN` | `PostgresDsn` | — | PostgreSQL connection string (required) |
| `DB_ECHO` | `bool` | `False` | SQLAlchemy query logging |
| `LIBRARY_DIR` | `Path` | — | Target root for organized music files (required; must be empty on first run) |
| `DUPLICATES_FILEPATH` | `Path` | `data/duplicates.json` | Output path for deduplication results |
| `NAVIDROME_DB_DSN` | `str \| None` | `None` | Path to Navidrome's SQLite database |

PostgreSQL runs on port **5437** (see `compose.yml`). The `initdb/` directory contains DB initialization scripts.

## Conventions

- Line length: 110 characters (Ruff)
- Imports: first-party `airdrome` group separated; combined-as imports; two blank lines after imports block
- All datetimes are timezone-aware UTC
- Database operations use `get_or_create()` patterns for idempotency; re-running commands is safe
