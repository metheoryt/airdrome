# AGENTS.md

Guidance for AI coding agents (Claude Code and others) working in this repository.
These instructions OVERRIDE default behavior — follow them exactly.

## Project

Airdrome migrates music libraries and scrobble/play history from cloud services
(Spotify, Apple Music, Last.fm, ListenBrainz) to
[Navidrome](https://www.navidrome.org/), a self-hosted music server. It ingests
metadata, deduplicates tracks, organizes files on disk, and syncs play history.

This is a single-user, personal-data project. There is no production database to
protect: schema changes recreate the DB rather than migrate it (see *Migrations*).

## Commands

The interpreter lives in `.venv`. Any command using packages from `pyproject.toml`
must run via `uv run <cmd>` or with the virtualenv activated
(`.venv/Scripts/activate` on Windows, `.venv/bin/activate` on Unix).

```bash
# Install (requires Python 3.14+)
uv sync

# Start PostgreSQL (port 5437, see compose.yml)
docker compose up -d

# Lint / format (also wired as pre-commit hooks: ruff check --fix, ruff format)
ruff check .
ruff format .

# Run tests (requires PostgreSQL running)
uv run pytest

# Run CLI
airdrome --help

# Migrations (schema auto-applied on CLI startup via upgrade_to_head())
uv run alembic revision --autogenerate -m "<message>"  # after editing models
uv run alembic upgrade head                             # apply pending migrations
uv run alembic downgrade -1                             # roll back one migration
```

## CLI surface

Four core verbs plus two maintenance groups. End-to-end flow:
`import ./exports/...` → `resolve` → `library organize` → `navidrome push` / `navidrome playlists`.

- **`airdrome import <path>...`** — auto-detect each source and ingest its
  tracks/playlists/scrobbles. Dumb and per-source; pass many paths or a directory.
  Flags: `--no-tracks` / `--no-playlists` / `--no-scrobbles`, `--as <source>`
  (force a source, applies to every path), `--dry-run`/`-n`. All importers are
  resolved up front so an unrecognized/ambiguous path fails before anything is written.
- **`airdrome resolve`** — build the canonical graph from *everything imported*
  (run once, after all imports). Runs in dependency order: `do_unify` →
  `augment_aliases` → `match_aliases` → `copy_plays`. Idempotent; re-running only
  fills gaps. Flags: `--threshold`/`-t` (default 0.4), `--dry-run`.
- **`airdrome library`**
  - `organize` — move (default) or `--copy` bound files into the structured library.
  - `deduplicate` — interactive TUI to review duplicate groups and pick canons.
    Defaults to three loose single-field sets (artist / album_artist / album).
  - `auto-deduplicate` — batch rebuild of `Track.canon_id` from N flag-sets +
    stored manual overrides. No `--set` means one all-fields set.
  - `export-duplicates` / `import-duplicates` — round-trip confirmed dedup groups
    to/from a portable JSON file (default `DUPLICATES_FILEPATH`); idempotent upsert.
  - `renormalize` — recompute `_norm` fields on tracks, aliases, and files.

  `deduplicate` and `auto-deduplicate` share one grouping engine
  (`compute_auto_dedup_groups` + `merge_overlapping_groups`) and the same
  `--set`/`-s` (repeatable, comma-separated fields; title always implicit) and
  `--canon`/`-c` (`added` = earliest added, `year` = oldest release) flags.
- **`airdrome navidrome`** (both commands require Navidrome stopped; they guard on
  the port and prompt unless `--yes`/`-y`)
  - `push` — push play counts + ratings for `NAVIDROME_USER`.
  - `playlists` — 3-way-merge every playlist between Airdrome and Navidrome.

## Architecture

### Data flow

1. **Import** (`airdrome/ingest/`) — `import` auto-detects each source and writes
   raw `SourceTrack`/`SourcePlaylist`/`TrackFile` and `TrackAlias`/`TrackAliasScrobble`
   rows. Per-source format parsers live under `airdrome/cloud/` (Spotify JSON,
   Last.fm CSV, ListenBrainz JSON, iTunes XML, Apple Media Services) plus a local
   music-folder scanner.
2. **Resolve** (`airdrome/library/unify.py`, `airdrome/scrobbles/`) — unify source
   rows into canonical `Track`/`Playlist` + bind files, then augment, fuzzy-match
   (`pg_trgm`), and copy scrobbles into `TrackPlay` history. Needs the full picture.
3. **Organize** (`airdrome/library/organize.py`) — move/copy bound files into a
   structured directory; `select_main` picks the best copy (bitrate, then ext).
4. **Deduplicate** (`airdrome/normalize/dedup/`) — group duplicate tracks via
   trigram fuzzy matching; duplicates link to a canonical track via `Track.canon_id`.
   Confirmed groups live in `dedupgroup`/`dedupgroupmember` (import identity = the
   member-hash multiset, not the label).
5. **Sync** (`airdrome/navidrome/`, `airdrome/playlists/`) — push play counts +
   ratings and merge playlists into Navidrome's SQLite DB.

### Import / Source design (implemented)

- One `Importer` ABC in `airdrome/ingest/base.py` (named `Importer`, not `Source`,
  to avoid colliding with the `Source` enum): `provides: ClassVar[DataKind]`,
  `classmethod detect(path)`, `__init__(path)`, no-op `import_tracks/playlists/scrobbles`,
  and `ingest(s, kinds)` which calls them in dependency order (tracks → playlists →
  scrobbles). `sources.py` holds the concrete importers; `registry.py` provides
  `detect`/`BY_NAME` and is loud on 0 or >1 matches.
- `Platform` + `Provider` are collapsed into one `Source` StrEnum (APPLE_XML,
  APPLE_MS, SPOTIFY, LASTFM, LISTENBRAINZ, NAVIDROME) with a `.service` property
  (APPLE_XML/APPLE_MS → "apple"). Apple is one *service* but two *export providers*;
  the (provider, source_id) split is load-bearing — fine granularity is canonical,
  coarse "service" is derived.
- Apple Media Services exports bury activity files inside a *nested*
  `Apple_Media_Services.zip` (layout varies), so `AppleMsImporter.detect` recurses
  into nested zips — a shallow `namelist` check misses them.
- Folder scan writes only `TrackFile`; `do_unify` promotes orphan `TrackFile`s to
  `Track` (no `SourceTrack(FOLDER)` layer, no FOLDER enum value).
- Spotify plays under 30 seconds are excluded (per-source config on the importer).

### Session lifecycle (implemented)

Sessions are created centrally in the root `app` callback
(`airdrome/terminal/app.py`) via `ctx.with_resource(Session(engine))` and stored in
`ctx.obj` as `AppState(session, dry_run)` (defined in `airdrome/terminal/state.py`).
A `ctx.call_on_close` hook commits, or rolls back when `--dry-run` is set.

**When adding a CLI command:** take `ctx: typer.Context` as the first param, read
`state: AppState = ctx.obj`, set `state.dry_run = dry_run` if the command exposes it,
and pass `state.session` to business logic. Business logic functions accept
`s: Session` and use `s.flush()` for intermediate persistence — they must **not** call
`s.commit()`. Known intentional exceptions: the interactive `Deduplicator` commits on
the user's 'c' keypress; `auto_deduplicate` commits (with a behavioral `dry_run` param);
Navidrome sync manages its own SQLite session locally and is not affected by `--dry-run`.

`upgrade_to_head()` runs inside the `main` callback (not at module import), so
`--help` and arg errors don't hit the DB.

### Key models (`airdrome/models.py`)

- **`Track`** — canonical track; normalized title/artist/album fields (`_norm`
  suffix); self-referential `canon_id` for deduplication. Unique on
  (title, artist, album, album_artist).
- **`TrackFile`** — audio file on disk with Mutagen-extracted metadata; `is_main`
  flags the best-bitrate copy; `library_path` is the organized destination;
  `enrich()` reads tags from `absolute_path` into the row.
- **`TrackAlias`** — a scrobble source entry (one per unique title+artist+album+platform).
- **`TrackAliasScrobble`** — an individual play event (timestamp, platform, duration).

### Normalization & matching (`airdrome/normalize/`, `airdrome/match.py`)

All text fields have `_norm` variants (lowercased, accents stripped). Fuzzy
deduplication and alias matching use PostgreSQL trigram similarity (`pg_trgm`) with
weighted scoring: artist/album_artist 75%, album 25%. Threshold defaults to 0.4,
tunable via CLI flag.

### Navidrome integration (`airdrome/navidrome/`)

`navidrome/models.py` maps Navidrome's SQLite schema read-only (`MediaFile`,
`Annotation`, `Playlist`, …). Sync writes to `Annotation` to record play counts and
ratings per user. WAL is checkpointed before writes.

Track-sync is dedup-group-aware (`sync/tracks.py`): exactly one `is_main` file exists
per dedup group, owned by whichever member had the best copy (often a twin, not the
canon). Both writers keep this invariant: organize picks it across canon + twins, and
`recompute_main_files` re-picks it whenever the canon graph changes (end of
`auto_deduplicate`, interactive `apply_changes`) so a merge never leaves two mains.
Selection is the shared `TrackGroup.select_main_file` (bitrate, then container). It only
flips the flag — files already on disk are not relocated (that is the reconcile roadmap).
The MediaFile is keyed off that owner, but plays are summed and
rating/loved are aggregated (max rating, any-loved) across the **whole group**
(`_group_members` = canon + twins) — otherwise plays attached to other members would be
dropped. Playlist sync resolves canon separately via `_resolve_canonical`.

### Migrations

No production DB exists — recreate on schema change. All prior migrations are
squashed into a single autogenerated initial schema (which also runs
`CREATE EXTENSION pg_trgm`). Workflow on a model change: edit models → delete the
existing migration → `alembic revision --autogenerate` against an empty DB → recreate
or stamp. Schema is auto-applied at CLI startup.

## Configuration (`airdrome/conf.py`)

A Pydantic-settings `Settings` class loads from a `.env` at the project root; the
singleton `settings = Settings()` is imported wherever config is needed.

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `DB_DSN` | `PostgresDsn` | — | PostgreSQL connection string (required) |
| `DB_ECHO` | `bool` | `False` | SQLAlchemy query logging |
| `LIBRARY_DIR` | `Path` | — | Target root for organized files (required; must be empty on first run) |
| `DUPLICATES_FILEPATH` | `Path` | `data/duplicates.json` | Default file for `library {import,export}-duplicates` |
| `NAVIDROME_DB_DSN` | `str \| None` | `None` | Path to Navidrome's SQLite database |
| `NAVIDROME_USER` | `str \| None` | `None` | Navidrome username that owns synced play counts/ratings |
| `NAVIDROME_PORT` | `int` | `4533` | Port the `navidrome` commands probe to confirm the server is stopped |

PostgreSQL runs on port **5437**. The `initdb/` directory holds DB init scripts.

> Note: the `duplicates_filepath` docstring in `conf.py` still calls itself "legacy",
> but it is live — it is the default path for `export-duplicates`/`import-duplicates`.

## Workflow

- For any task beyond a simple bugfix, start a new branch from `main`.
- Cover changes with meaningful tests. Lean on pytest and its fixtures heavily.
- Give every function and method a one-line docstring.
- Comment non-obvious code, explaining the decision made — the more complicated the
  situation, the more explicit the comment.
- While working, spot and call out places that can be simplified.

## Conventions

- Line length: 110 (Ruff). Target `py314`. Lint rules: `E,F,I,W,UP,B,SIM,C4,PIE,RUF`.
- Imports: first-party `airdrome` group separated; `factories` is known-local;
  combine-as imports; two blank lines after the imports block; no split-on-trailing-comma.
- All datetimes are timezone-aware UTC.
- Database operations use `get_or_create()` for idempotency; re-running commands is safe.
- `typer.Argument`/`typer.Option` sentinels as defaults are the intended idiom
  (exempt from B008); optional no-op ingest/sync hooks are exempt from B027.
- `/data/`, `/navi/`, `/dist/`, `.env` are gitignored. Treat `data/` (e.g.
  `data/duplicates.json`) as local runtime state the user backs up separately — do
  not optimize its serialization for "cleaner git diffs"; only round-trip correctness matters.

## Dedup tuning notes

- Recommended `auto-deduplicate` field sets (precise yet mass):
  `-s "artist,duration" -s "artist,year" -s "album_artist,duration"`. On the live DB
  this reached ~98% of the reckless `artist`-only ceiling while gating every merge on
  a matching duration OR year, avoiding collapse of edits/remixes/live cuts.
- Bare `-s "artist"` is dangerous (no discriminator). A conservative start is the
  2-set `artist,duration` + `artist,year`. "all fields" only finds exact-dup files
  and misses the cross-album point.
- `compute_auto_dedup_groups` skips a track when *all* selected non-title key fields
  are blank (fixes loose single-field sets collapsing blank-keyed tracks).
- `loved` was deliberately removed from canon ordering — a group's loved status is
  derived from the whole group, so it must not decide which member is canon. The
  merge re-query must use the same `canon_order(strategy)` ordering or `group[0]`
  disagrees between single and merged groups.

## Roadmap — filesystem ⇄ Airdrome reconcile (agreed, NOT yet built)

A settled design exists for self-repairing organize, a watch folder, and a tag
`reconcile` pipeline. Core mental model is three hops; organize is only the last:
`file tags --enrich--> TrackFile metadata --unify--> Track identity --organize--> disk location`.
Tag changes do nothing physical until they reach the Track.

Settled decisions:
- Source roots are **not** stored; `import <path>` stays stateless. Re-scan = re-run
  import. Adds-only re-scan already works; delete/move detection is deferred.
- Source-of-truth is **both** copy and move, configurable; reconcile must work either way.
- Idempotent/self-repairing organize via a per-file location state machine: compute
  `desired` from Track metadata + role (`is_main`); move from `absolute_path` (current
  location), not always `source_path` — this enables re-organizing already-placed files
  and self-heal. If a file is nowhere, **report missing — do not fabricate**.
  Self-heal (re-copy from source) only works in copy mode.
- Optional `copies_dir: Path | None` setting (default `library_dir/Copies`).
- Quality upgrades fall out for free: a higher-bitrate drop with identical tags hits
  the existing Track via `get_or_create`, attaches as a 2nd `TrackFile`, and organize
  promotes it. No fuzzy match involved.
- Watch matching uses strict full-metadata identity, **no trigram** (trigram dedup
  stays a separate batch step).
- Add a `content_hash` column on `TrackFile` (full-file md5, computed in enrich) for
  watch idempotency. Consequence: "same audio, corrected tags" is treated as a new file.
- `watch <folder>`: poll-on-a-timer first (move mode, drains the folder), real-time
  `watchdog` later behind the same function.
- Re-enrich is its own pass (iterate existing `TrackFile`s, re-read from
  `absolute_path`), not via the `source_path`-keyed `scan_file` (post-move source_path
  is stale → spurious duplicate files).
- unify re-bind is deferred: re-enrich only updates `TrackFile` fields; an in-place
  tag edit that changes identity won't relocate the file until re-bind exists.

Build order (each its own branch + tests): (1) self-repairing organize state machine,
(2) `content_hash` column, (3) `ingest_one()` per-file pipeline + `watch` + `reconcile`.
