# AGENTS.md

Guidance for AI coding agents working in this repository. These instructions OVERRIDE
default behavior — follow them exactly.

## Project

Airdrome migrates a music library plus scrobble/play history from cloud services
(Spotify, Apple Music, Last.fm, ListenBrainz) into [Navidrome](https://www.navidrome.org/),
a self-hosted music server: ingest metadata, deduplicate tracks, organize files on disk,
sync play history and playlists.

Single-user, personal-data project. There is no production database to protect — schema
changes recreate the DB rather than migrate it (see *Migrations*).

## Commands

The interpreter lives in `.venv`. Anything using project packages runs via `uv run <cmd>`
or with the venv activated (`.venv/Scripts/activate` on Windows).

```bash
uv sync                       # install (Python 3.14+)
docker compose up -d          # PostgreSQL on port 5437 (provides pg_trgm)
ruff check . && ruff format .  # lint + format (also pre-commit hooks)
uv run pytest                 # tests (need PostgreSQL up)
airdrome --help
```

## CLI surface

End-to-end flow: `import <path>...` → `resolve` → `library organize` → `library
auto-deduplicate` → `navidrome push` / `navidrome playlists`. Every write command is
idempotent and takes `--dry-run`/`-n` (rolls back instead of committing). Run any command
with `--help` for flags.

- **`import <path>...`** — auto-detect each source and ingest its tracks/playlists/scrobbles.
  Per-source and dumb; pass many paths or a directory. `--as <source>` forces a source for
  every path; `--no-tracks`/`--no-playlists`/`--no-scrobbles` skip a kind. All importers
  resolve up front, so an unrecognized/ambiguous path fails before anything is written.
- **`resolve`** — build the canonical graph from *everything imported* (run once, after all
  imports). Order: `do_unify` → `augment_aliases` → `match_aliases` → `copy_plays`.
  `-t/--threshold` (fuzzy alias match, default 0.4); `-m/--merge-playlists` (collapse
  same-name playlists, newest anchors); `--rebuild-playlists` (drop + rebuild canonical
  playlists from source, discarding backend-sync links).
- **`library`**
  - `organize` — move (default) or `--copy` bound files into `LIBRARY_DIR`. `select_main`
    picks the best copy (bitrate, then container).
  - `deduplicate` — interactive TUI to review duplicate groups and pick canons.
  - `auto-deduplicate` — batch rebuild of `Track.canon_id` from N flag-sets + stored manual
    overrides. Shares `-s/--set` (repeatable, comma-separated fields; title always implicit)
    and `-c/--canon` (`added`/`year`) with `deduplicate`.
  - `export-duplicates`/`import-duplicates` — round-trip confirmed dedup groups to/from JSON
    (default `DUPLICATES_FILEPATH`); idempotent upsert keyed on the member set.
  - `renormalize` — recompute `_norm` fields on tracks, aliases, and files.
- **`navidrome`** (both require Navidrome stopped; they probe `NAVIDROME_PORT` and prompt
  unless `-y/--yes`, then write its SQLite DB directly)
  - `push` — play counts + ratings for `NAVIDROME_USER`.
  - `playlists` — 3-way merge every playlist between Airdrome and Navidrome.

## Architecture

### Data flow

1. **Import** (`ingest/`) — `import` auto-detects each source and writes raw
   `SourceTrack`/`SourcePlaylist`/`TrackFile` + `TrackAlias`/`TrackAliasScrobble` rows.
   Format parsers live under `cloud/` (Spotify, Last.fm, ListenBrainz, iTunes XML, Apple
   Media Services) plus a local folder scanner.
2. **Resolve** (`library/unify.py`, `scrobbles/`) — unify source rows into canonical
   `Track`/`Playlist`, bind files, then augment, fuzzy-match (`pg_trgm`), and copy scrobbles
   into `TrackPlay` history. Needs the full picture.
3. **Organize** (`library/organize.py`) — move/copy bound files into a structured directory.
4. **Deduplicate** (`normalize/dedup/`) — group duplicates via trigram matching; twins link
   to a canon via `Track.canon_id`. Confirmed groups live in `dedupgroup`/`dedupgroupmember`
   (import identity = the member-hash multiset, not the label).
5. **Sync** (`navidrome/`, `playlists/`) — push play counts + ratings, merge playlists into
   Navidrome's SQLite DB.

### Import / source design

- One `Importer` ABC in `ingest/base.py` (named `Importer`, not `Source`, to avoid colliding
  with the `Source` enum): `provides: ClassVar[DataKind]`, `classmethod detect(path)`,
  no-op `import_tracks/playlists/scrobbles`, and `ingest()` running them in dependency order.
  `sources.py` holds the concrete importers; `registry.py` (`detect`/`BY_NAME`) is loud on 0
  or >1 matches.
- `Source` is one StrEnum collapsing platform + provider (APPLE_XML, APPLE_MS, SPOTIFY,
  LASTFM, LISTENBRAINZ, NAVIDROME) with a `.service` property (both Apple values → "apple").
  Apple is one *service* but two *export providers*; the (provider, source_id) split is
  load-bearing — fine granularity is canonical, coarse "service" is derived.
- Apple Media Services exports bury activity files in a *nested* zip (layout varies), so
  `AppleMsImporter.detect` recurses into nested zips.
- Folder scan writes only `TrackFile`; `do_unify` promotes orphan `TrackFile`s to `Track`
  (no `SourceTrack(FOLDER)` layer, no FOLDER enum value).
- Spotify plays under 30 seconds are excluded.

### Session lifecycle

Sessions are created centrally in the root `app` callback (`terminal/app.py`) and stored as
`AppState(session, dry_run)` on `ctx.obj`; a close hook commits, or rolls back under
`--dry-run`. `upgrade_to_head()` runs inside the callback (not at import), so `--help` and
arg errors don't hit the DB. `expire_on_commit=False` keeps ORM objects live across the
interactive deduplicator's repeated commits.

**When adding a CLI command:** take `ctx: typer.Context` first, read `state: AppState =
ctx.obj`, set `state.dry_run = dry_run` if exposed, pass `state.session` to business logic.
Business logic takes `s: Session` and uses `s.flush()` for intermediate persistence — it
must **not** call `s.commit()`. Intentional exceptions: the interactive `Deduplicator`
commits on the user's keypress; `auto_deduplicate` commits (with a behavioral `dry_run`);
Navidrome sync manages its own SQLite session and ignores `--dry-run`.

### Key models (`models.py`)

- **`Track`** — canonical track; normalized `_norm` title/artist/album fields; unique on
  (title, artist, album, album_artist). Self-referential `canon_id` links a twin to its
  canon — and is *terminal* (a canon is never itself a twin; no chains), so readers resolve
  with a single hop.
- **`TrackFile`** — audio file on disk with Mutagen-extracted metadata; `is_main` flags the
  best-bitrate copy; `library_path` is the organized destination; `enrich()` reads tags from
  `absolute_path` into the row.
- **`TrackAlias`** — one scrobble-source entry per unique title+artist+album+platform.
- **`TrackAliasScrobble`** — an individual play event; `copy_plays` materializes matched
  aliases into canonical **`TrackPlay`** history.

### Normalization & matching (`normalize/`, `match.py`)

All text fields have `_norm` variants (lowercased, accents stripped). Fuzzy dedup and alias
matching use PostgreSQL trigram similarity (`pg_trgm`), weighted artist/album_artist 75%,
album 25%. Threshold defaults to 0.4.

### Navidrome integration (`navidrome/`)

`navidrome/models.py` maps Navidrome's SQLite schema read-only; sync writes `Annotation`
(play counts, ratings) per user and checkpoints WAL before writes. Track sync is
dedup-group-aware: exactly one `is_main` file exists per group (owned by whichever member
has the best copy — often a twin, not the canon), but plays are **summed** and rating/loved
**aggregated** across the whole group, so plays on other members aren't dropped. Both writers
keep the one-main invariant — `organize` picks it across canon+twins, and
`recompute_main_files` re-picks it whenever the canon graph changes. Selection only flips the
flag; files already on disk are not relocated (that is the reconcile roadmap).

### Migrations

No production DB — recreate on schema change. All prior migrations are squashed into one
autogenerated initial schema (which also runs `CREATE EXTENSION pg_trgm`). On a model change:
edit models → delete the existing migration → `uv run alembic revision --autogenerate`
against an empty DB. Schema is auto-applied at CLI startup.

## Configuration (`conf.py`)

A pydantic-settings `Settings` loads from `.env` at the project root; the `settings`
singleton is imported wherever config is needed.

| Variable | Default | Purpose |
|---|---|---|
| `DB_DSN` | — | PostgreSQL DSN (required) |
| `DB_ECHO` | `False` | SQLAlchemy query logging |
| `LIBRARY_DIR` | — | Root for organized files (required; empty on first run) |
| `DUPLICATES_FILEPATH` | `data/duplicates.json` | Default file for `library {im,ex}port-duplicates` |
| `NAVIDROME_DB_DSN` | `None` | Path to Navidrome's SQLite DB |
| `NAVIDROME_USER` | `None` | Navidrome user that owns synced play counts/ratings |
| `NAVIDROME_PORT` | `4533` | Port the `navidrome` commands probe to confirm the server is stopped |

`/data/`, `/navi/`, `/dist/`, `.env` are gitignored. Treat `data/` as local runtime state the
user backs up separately — don't optimize its serialization for "cleaner git diffs"; only
round-trip correctness matters.

## Workflow

- For any task beyond a simple bugfix, start a new branch from `main`.
- Cover changes with meaningful tests; lean on pytest fixtures heavily.
- Give every function and method a one-line docstring.
- Comment non-obvious code, explaining the decision — the more complicated, the more explicit.
- Call out places that can be simplified as you go.

## Conventions

- Line length 110 (Ruff), target `py314`, rules `E,F,I,W,UP,B,SIM,C4,PIE,RUF`.
- Imports: first-party `airdrome` group separated; `factories` known-local; two blank lines
  after the imports block.
- All datetimes are timezone-aware UTC.
- Use `get_or_create()` for idempotency; re-running commands is safe.
- `typer.Argument`/`typer.Option` sentinels as defaults are intended (exempt from B008);
  optional no-op ingest/sync hooks are exempt from B027.

## Dedup tuning notes

- Recommended `auto-deduplicate` sets (precise yet mass): `-s "artist,duration" -s
  "artist,year" -s "album_artist,duration"` — reaches ~98% of the reckless `artist`-only
  ceiling while gating every merge on a matching duration OR year, so edits/remixes/live cuts
  don't collapse.
- Bare `-s "artist"` is dangerous (no discriminator). "All fields" only finds exact-dup files.
- `compute_auto_dedup_groups` skips a track when *all* selected non-title key fields are blank.
- `loved` is deliberately out of canon ordering — a group's loved status is derived from the
  whole group, so it must not decide which member is canon. The merge re-query must use the
  same `canon_order(strategy)` or single vs. merged groups disagree on `group[0]`.

## Roadmap

Forward-looking work — to-do ideas, open design questions, and agreed-but-unbuilt designs —
lives in [ROADMAP.md](ROADMAP.md) (playlist management, a Telegram management bot, and the
filesystem ⇄ Airdrome reconcile pipeline). Read it at the start of any non-trivial task.

**When a new idea surfaces** in a dialog — a feature, a "we should eventually…", or a design
we settle on but won't build now — **ask the user whether to add it to ROADMAP.md**, and
record it there if they agree. When an item ships, fold its durable design into this file and
remove it from the roadmap.
