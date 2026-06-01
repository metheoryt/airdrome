# Airdrome

Airdrome migrates music libraries and scrobble/play history from cloud services
(Spotify, Apple Music, Last.fm, ListenBrainz) to [Navidrome](https://www.navidrome.org/),
a self-hosted music server. It ingests metadata, deduplicates tracks, organizes files
on disk, and syncs play counts, ratings, and playlists.

## Requirements

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/)
- Docker (for the bundled PostgreSQL, which provides the `pg_trgm` fuzzy matching used
  throughout deduplication and alias matching)

## Setup

```bash
# Install dependencies into .venv
uv sync

# Start PostgreSQL (listens on port 5437)
docker compose up -d
```

Then create a `.env` file at the project root (see [Configuration](#configuration)). A minimal
config matching the bundled `compose.yml`:

```ini
DB_DSN = postgresql+psycopg://postgres:postgres@localhost:5437/postgres
LIBRARY_DIR = /path/to/your/organized/library
```

The database schema is applied automatically on the first CLI invocation — there's no
manual migration step for normal use.

## Configuration

All settings load from a `.env` file at the project root (`airdrome/conf.py`).

| Variable           | Required | Default | Purpose                                                                                             |
|--------------------|----------|---------|-----------------------------------------------------------------------------------------------------|
| `DB_DSN`           | ✅        | —       | PostgreSQL connection string, e.g. `postgresql+psycopg://postgres:postgres@localhost:5437/postgres` |
| `DB_ECHO`          |          | `False` | Log every SQL statement (debugging)                                                                 |
| `LIBRARY_DIR`      | ✅        | —       | Destination root for organized files. Must be empty on a fresh install.                             |
| `DUPLICATES_FILEPATH` |       | `data/duplicates.json` | Default file for `library export-duplicates` / `import-duplicates`                   |
| `NAVIDROME_DB_DSN` |          | `None`  | Path to Navidrome's SQLite database (required for the `navidrome` commands)                         |
| `NAVIDROME_USER`   |          | `None`  | Navidrome username that play counts / ratings are written for                                       |
| `NAVIDROME_PORT`   |          | `4533`  | Port Airdrome probes to refuse syncing while Navidrome is running                                   |

## Supported sources

`airdrome import` auto-detects the source by inspecting the file/folder contents.
Use `--as <name>` to force one when detection is ambiguous or fails.

| `--as` name    | Source               | Format                                 | Provides                     |
|----------------|----------------------|----------------------------------------|------------------------------|
| `apple_xml`    | Apple iTunes XML     | `Library.xml` plist                    | tracks, playlists            |
| `apple_ms`     | Apple Media Services | export zip/folder                      | tracks, playlists, scrobbles |
| `spotify`      | Spotify              | extended-streaming-history JSON        | scrobbles                    |
| `listenbrainz` | ListenBrainz         | `.jsonl` export                        | scrobbles                    |
| `lastfm`       | Last.fm              | CSV export (`artist,album,track,date`) | scrobbles                    |
| `folder`       | Music folder         | directory of `.mp3`/`.m4a`/`.flac`     | tracks                       |

## Migration pipeline

A full migration runs roughly in this order. Every command is idempotent — re-running is
safe and only fills gaps. Add `--dry-run`/`-n` to any write command to roll back instead of
committing.

```bash
# 1. Import every source you have (one invocation, any mix of exports / folders)
airdrome import ./exports/itunes/Library.xml ./exports/Apple_Media_Services.zip \
                ./exports/spotify_history/ /mnt/music

# 2. Build the canonical graph from everything imported: unify Track/Playlist
#    records, bind on-disk files, then augment/match/copy scrobbles into play history.
airdrome resolve                   # --threshold tunes fuzzy matching; --reset rebuilds

# 3. Organize the bound files into LIBRARY_DIR (move, or --copy to keep originals)
airdrome library organize          # add --copy to copy, --reset to redo from scratch

# 4. Deduplicate canonical tracks (fuzzy trigram matching)
airdrome library auto-deduplicate              # automatic, flag-set driven
airdrome library deduplicate                   # interactive review/override
airdrome library export-duplicates             # back up confirmed groups to JSON (re-import after a DB rebuild)

# 5. Push to Navidrome (stop Navidrome first — these write its SQLite DB directly)
airdrome navidrome push             # play counts + ratings
airdrome navidrome playlists        # 3-way playlist merge
```

## Command reference

Run any command with `--help` for its full options.

### `airdrome import <path>...`

Auto-detect the source at each `<path>` and import its tracks, playlists, and scrobbles.
Accepts any number of paths; each is detected and ingested independently.

- `--as <name>` — force a source for every path (see table above)
- `--no-tracks` / `--no-playlists` / `--no-scrobbles` — skip a data kind
- `--dry-run`, `-n`

### `airdrome resolve`

Build the canonical graph from everything imported — run once, after all imports. Unifies source
tracks/playlists into canonical `Track`/`Playlist` records and binds on-disk files, then augments,
fuzzy-matches, and materializes scrobbles into `TrackPlay` play history. Idempotent.

- `--threshold`, `-t` — fuzzy alias-match similarity (default `0.4`)
- `--reset`, `-r` — rebuild canonical playlists and re-match scrobbles from scratch
- `--dry-run`, `-n`

### `airdrome library`

- `organize` — move/copy bound files into `LIBRARY_DIR` (`--copy`, `--reset`)
- `auto-deduplicate` — rebuild `canon_id` automatically; `--set "artist,album,year"` defines a
  comparison flag-set (repeatable; multiple sets union-merge their groups). `--canon`/`-c`
  picks which group member becomes canon: `added` (earliest added, default) or `year` (oldest release)
- `deduplicate` — interactive duplicate review (TUI). Shares `--set`/`--canon` with `auto-deduplicate`
  (defaults to three loose single-field sets — `artist`, `album_artist`, `album` — for broad recall);
  `--match <substring>` filters the groups shown
- `export-duplicates` / `import-duplicates` — round-trip confirmed duplicate groups to a portable
  JSON file (default `DUPLICATES_FILEPATH`). Import is idempotent and matches groups by their member
  set, so your manual decisions survive a database rebuild
- `renormalize` — recompute the `_norm` text fields for tracks, aliases, and files

### `airdrome navidrome`

> ⚠️ These write directly to Navidrome's SQLite database. **Stop Navidrome first** — the CLI
> refuses to run while it's listening on `NAVIDROME_PORT`. Pass `--yes`/`-y` to skip the prompt.

- `push` — push play counts and ratings for `NAVIDROME_USER` (`--reset`)
- `playlists` — 3-way merge every playlist between Airdrome and Navidrome

## Development

```bash
ruff check .          # lint
ruff format .         # format
uv run pytest         # tests (require PostgreSQL running)

# Migrations — only when changing airdrome/models.py
uv run alembic revision --autogenerate -m "<message>"
uv run alembic upgrade head
uv run alembic downgrade -1
```
