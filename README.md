# Airdrome

**Move your music library — and a decade of listening history — off Spotify, Apple Music,
Last.fm and ListenBrainz onto self-hosted [Navidrome](https://www.navidrome.org/).**

Leaving a streaming service is easy. Leaving without losing anything is not. Your files sit
in one pile, your play counts live in a Spotify JSON export, your ratings in an Apple
`Library.xml`, your playlists in three places that disagree with each other — and none of
them agree on how to spell an artist's name. Navidrome will happily serve the files and
knows nothing about the rest.

Airdrome is the missing step. It ingests every export you have, resolves the same song
across all of them into one canonical track, binds those tracks to the actual files on
disk, organizes the library, and writes the play counts, ratings and playlists into
Navidrome. Every stage is idempotent: run it, import another export, run it again.

`airdrome status` tells you where a migration stands at any point (example output):

```console
$ airdrome status
Environment
Database     connected postgresql+psycopg://localhost:5437/postgres
Library dir  /srv/music (12,431 files)
Navidrome    configured, not running

Imported
Source tracks     18,204
Source playlists  313
Scrobbles         214,880

Canonical (land)
Tracks           12,908
Aliases matched  9,731 / 10,402
Plays            198,447
Playlists        296

Files
Bound to tracks    12,431 / 12,908
Organized on disk  12,431

Dedup
Twins             1,204 in 517 group(s)
Confirmed groups  488

Synced to backends
Playlist links  296
```

## How it works

The interesting problem isn't reading the exports — it's that **no two sources identify a
song the same way**, and a scrobble from 2014 has nothing but three strings of text to
connect it to a FLAC on your disk.

- **A canonical hub.** Every source is imported raw and untouched (`SourceTrack`,
  `SourcePlaylist`, `TrackAlias`), then *landed* into one canonical `Track`/`Playlist`
  graph. Sources are never edited in place, so a re-import is always safe and the raw
  record stays available when a matching rule changes.
- **Fuzzy matching in the database.** Scrobble metadata is bound to canonical tracks with
  PostgreSQL trigram similarity (`pg_trgm`) over normalized text, with a tunable threshold —
  so "Sigur Rós — Untitled #1" and "Sigur Ros - Untitled 1 (Vaka)" land on the same track
  instead of two.
- **Deduplication you can correct.** The same album ripped twice, plus a stream copy, plus
  the remaster — grouped by configurable flag-sets (artist/album/year) and collapsed onto a
  canon. Automatic where it's confident, an interactive review pass where it isn't, and
  your manual decisions persist as overrides that survive a full database rebuild.
- **Playlist reconcile, not playlist push.** Each remote gets a per-playlist base snapshot,
  so a diff is computed against *what that remote last saw* rather than against the current
  hub. Downstream deletes stick, a re-import doesn't resurrect a track you removed, and a
  genuine conflict resolves to the remote that edited that track last — no prompts.
- **Idempotent stages.** Every command fills gaps rather than redoing work, which is what
  makes a migration you run over several weeks, as exports trickle in, actually tractable.

Design rationale, rejected alternatives and the measured data behind each decision live in
[`docs/design/`](docs/design/).

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
| `DUPLICATES_FILEPATH` |       | `data/duplicates.json` | Default file for `dedup-export` / `dedup-import`                                     |
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
airdrome land                      # --threshold tunes fuzzy matching; --merge-playlists collapses dupes

# 3. Organize the bound files into LIBRARY_DIR (copies by default; --move to move)
airdrome organize                  # add --move to move instead of copy

# 4. Deduplicate canonical tracks (fuzzy trigram matching)
airdrome dedup                                 # automatic, flag-set driven
airdrome dedup --review                        # batch, then open the TUI to adjust canons
airdrome dedup-export                          # back up confirmed groups to JSON (re-import after a DB rebuild)

# 5. Reconcile playlists across sources and Navidrome (stop Navidrome — it writes the backend)
airdrome sync all                   # sources -> canonical -> Navidrome; interactive on conflicts

# 6. Push play counts + ratings to Navidrome (stop Navidrome first — writes its SQLite DB directly)
airdrome navi push
```

## Command reference

Run any command with `--help` for its full options.

### `airdrome status`

Read-only snapshot of where you are. Shows config sanity (database connectivity, `LIBRARY_DIR`
state, whether Navidrome is configured and currently running) and per-stage counts (imported
sources, canonical tracks/aliases/plays/playlists, files bound and organized, dedup groups,
backend playlist links). Never writes and never applies migrations — safe to run anytime, and
it reports an unreachable database instead of failing.

### `airdrome import <path>...`

Auto-detect the source at each `<path>` and import its tracks, playlists, and scrobbles.
Accepts any number of paths; each is detected and ingested independently.

- `--as <name>` — force a source for every path (see table above)
- `--no-tracks` / `--no-playlists` / `--no-scrobbles` — skip a data kind
- `--dry-run`, `-n`

Global flags: `-v/--verbose` shows per-item detail (file picks, misses); `-q/--quiet`
suppresses non-essential output.

### `airdrome land`

Build the canonical graph from everything imported — run once, after all imports. Unifies source
tracks/playlists into canonical `Track`/`Playlist` records and binds on-disk files, then augments,
fuzzy-matches, and materializes scrobbles into `TrackPlay` play history. Idempotent.

- `--threshold`, `-t` — fuzzy alias-match similarity (default `0.4`)
- `--merge-playlists`, `-m` — collapse same-name playlists into one canonical (newest anchors)
- `--rebuild-playlists` — drop and rebuild canonical playlists from source (discards backend-sync links)
- `--dry-run`, `-n`

### `airdrome organize`

Copy (default) or move bound files into `LIBRARY_DIR`; picks the best copy (bitrate, then container)
as each track's main.

- `--move`, `-m` — move files instead of copying them
- `--dry-run`, `-n` — plan only: reports the paths and count a real run would produce, and still
  reports a missing source or an occupied destination, but writes nothing to disk. It cannot see a
  collision between two tracks that resolve to the *same* destination — a real run would hit that.

### `airdrome dedup`

Rebuild `canon_id` from flag-sets + stored manual overrides.

- `--set`/`-s` `"artist,album,year"` — comparison flag-set (repeatable; multiple sets union-merge
  their groups). With no `--set`, the recommended sets are used.
- `--canon`/`-c` — which group member becomes canon: `added` (earliest added, default) or `year`
  (oldest release)
- `--review`/`-r` — after the batch pass, open the interactive TUI to adjust canons; `--match
  <substring>` filters the groups shown. Choices persist as manual overrides feeding the next run.

### `airdrome dedup-export` / `airdrome dedup-import`

Round-trip confirmed duplicate groups to a portable JSON file (default `DUPLICATES_FILEPATH`).
Import is idempotent and matches groups by their member set, so your manual decisions survive a
database rebuild.

### `airdrome sync <remote>` / `airdrome sync all`

Reconcile playlists across remotes, with Airdrome as the source of truth. Cloud sources
(`apple_xml`, `apple_ms`) are read-only; `navidrome` is a read-write backend. `sync all` runs
sources first, then backends. Each playlist is merged against a per-remote base, so downstream
deletes stick and re-imports don't resurrect removed tracks. `sync` never prompts: when
remotes disagree on a track (one added it, another removed it), the **last remote that edited
that track** wins it — with `sync all`'s sources-then-backends order, a Navidrome edit beats a
source export. Every other edit still merges normally, and each auto-resolution is printed.

- `--dry-run`/`-n`
- `--yes`/`-y` — skip the Navidrome-stopped confirmation

> ⚠️ Any scope that includes `navidrome` writes its SQLite database directly. **Stop Navidrome
> first** — the CLI refuses to run while it's listening on `NAVIDROME_PORT`. `sync apple_*` is
> read-only and unguarded.

### `airdrome navi push`

> ⚠️ Writes directly to Navidrome's SQLite database. **Stop Navidrome first** — the CLI refuses
> to run while it's listening on `NAVIDROME_PORT`. Pass `--yes`/`-y` to skip the prompt.

Pushes play counts + ratings for `NAVIDROME_USER`. (Playlists are reconciled with `airdrome
sync`.)

### `airdrome maint renormalize`

Recompute the `_norm` text fields for tracks, aliases, and files (escape hatch for a
normalization-rule change, instead of a full reimport).

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
