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
Twins                1,204 in 517 group(s)
Decisions same       312
Decisions different  176
Canon choices        12
Superseded           4
Dead                 0

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
- **An identity ladder, strongest evidence first.** A file reaches its track by MusicBrainz
  recording id (read from tags, or looked up on AcoustID), then by its acoustic fingerprint
  (chromaprint), then by its tags — and every binding records which rung made it, so automatic
  merges can be listed and audited. Untagged rips work: the fingerprint says "you already have
  this recording" without any tagging first.
- **Fuzzy matching in the database.** Scrobble metadata is bound to canonical tracks with
  PostgreSQL trigram similarity (`pg_trgm`) over normalized text, with a tunable threshold —
  so "Sigur Rós — Untitled #1" and "Sigur Ros - Untitled 1 (Vaka)" land on the same track
  instead of two.
- **Deduplication you can correct.** The same album ripped twice, plus a stream copy, plus
  the remaster — grouped by configurable flag-sets (artist/album/year) and collapsed onto a
  canon. Automatic where it's confident; where it isn't, you decide through the MCP server, and
  your manual same/different decisions (the newest wins) survive a full database rebuild.
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
- `fpcalc` from [Chromaprint](https://acoustid.org/chromaprint) on the `PATH` (Debian/Ubuntu:
  `apt install libchromaprint-tools`) — fingerprints every audio file
- Optional: an AcoustID **application** key (register one at
  [acoustid.org/new-application](https://acoustid.org/new-application); the account's user key
  will not work) for `maint acoustid`

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
| `DUPLICATES_FILEPATH` |       | `LIBRARY_DIR/.airdrome/duplicates.json` | Where manual dedup decisions are mirrored (see below)          |
| `NAVIDROME_DB_DSN` |          | `None`  | Path to Navidrome's SQLite database (required for the `navidrome` commands)                         |
| `NAVIDROME_USER`   |          | `None`  | Navidrome username that play counts / ratings are written for                                       |
| `NAVIDROME_PORT`   |          | `4533`  | Port Airdrome probes to refuse syncing while Navidrome is running                                   |
| `ACOUSTID_CLIENT_KEY` |       | `None`  | AcoustID application key for fingerprint lookups (unset disables the lookup)                        |
| `AIRDROME_MCP_TOKEN` |        | `None`  | Secret token in the MCP server's URL (required for `airdrome mcp`; see *MCP server*)                |
| `AIRDROME_MCP_PORT` |         | `8765`  | Localhost port of the MCP server                                                                    |

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
| `folder`       | Music folder         | any directory of mp3/m4a/flac/ogg/opus/wav/aiff/wma | tracks          |

## Migration pipeline

A full migration runs roughly in this order. Every command is idempotent — re-running is
safe and only fills gaps. Add `--dry-run`/`-n` to any write command to roll back instead of
committing.

```bash
# 1. Import every source you have (one invocation, any mix of exports / folders)
airdrome import ./exports/itunes/Library.xml ./exports/Apple_Media_Services.zip \
                ./exports/spotify_history/ /mnt/music

# 1b. Read recording ids, md5 and fingerprints of the files (land also fingerprints what it needs)
airdrome maint enrich --workers 8
airdrome maint acoustid            # optional: fill missing recording ids from AcoustID

# 2. Build the canonical graph from everything imported: unify Track/Playlist
#    records, bind on-disk files, then augment/match/copy scrobbles into play history.
airdrome land                      # --threshold tunes fuzzy matching; --merge-playlists collapses dupes

# 3. Organize the bound files into LIBRARY_DIR (copies by default; --move to move)
airdrome organize                  # add --move to move instead of copy

# 4. Deduplicate canonical tracks (fuzzy trigram matching)
airdrome dedup                                 # automatic, flag-set driven, plus your manual decisions

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

Music folders are compared against the library first (unless it is empty). Repeats are collapsed
(identical bytes, identical audio), and each recording is reported once:

- **new** — nothing in the library matches it by recording id, fingerprint or tags
- **have** — the library already holds a copy at least as good (bitrate)
- **upgrade** — a higher-bitrate copy than any the library has; `organize` then puts it in
  `Library/` and moves the old main file to `Copies/`

Only new recordings and upgrades are imported; with `-n` the report is all you get. Files that are
not imported are remembered by path, size and mtime, so the next run does not fingerprint them
again (a `-n` run does not keep that memory). A new file with no title tag that matches nothing
is imported unbound and counted on its own line: tag it, then run `maint enrich` and `land`.

- `--as <name>` — force a source for every path (see table above)
- `--no-tracks` / `--no-playlists` / `--no-scrobbles` — skip a data kind
- `--dry-run`, `-n`

Global flags: `-v/--verbose` shows per-item detail (file picks, misses); `-q/--quiet`
suppresses non-essential output.

### `airdrome land`

Build the canonical graph from everything imported — run once, after all imports. Unifies source
tracks/playlists into canonical `Track`/`Playlist` records and binds on-disk files (Apple's own
file naming first, then recording id → fingerprint → tags for the rest), then augments, matches
scrobbles to tracks (submitted recording id → exact tags → fuzzy), and materializes them into
`TrackPlay` play history. Idempotent.

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

When an `import`ed upgrade replaces a main file of another format (say a FLAC for an MP3), Navidrome
sees the old file removed and a new one added. Once Navidrome has scanned the change, run
`navi push` (Navidrome stopped) so the play counts and ratings land on the new file.

### `airdrome dedup`

Rebuild `canon_id` from recording identity + flag-sets + your manual decisions. Tracks sharing a
recording id, or whose fingerprints match with durations within 2 s, are grouped first; a wider
duration gap is not joined. Two tracks you marked "different" are never joined automatically, and
two you marked "same" are always joined, even across recording ids (the newest decision wins when
they contradict). There is no interactive review: decisions are made through the MCP tools. In a
group joined only by a manual "same", the main file always comes from the canon's own version. Inside a group the original album is canon, unless you own ≥ 2 tracks of the
compilation/OST it is also on.

- `--set`/`-s` `"artist,album,year"` — comparison flag-set (repeatable; multiple sets union-merge
  their groups). With no `--set`, the recommended sets are used.
- `--canon`/`-c` — which group member becomes canon: `added` (earliest added, default) or `year`
  (oldest release)

### Your duplicate decisions survive a database rebuild

Deciding which of five copies of a song is the real one is work you do by hand, and the Postgres
database is disposable — it gets recreated whenever the schema changes. So every manual decision
(same / different pairs, canon choices) is mirrored to `LIBRARY_DIR/.airdrome/duplicates.json` the
moment it is saved, and `dedup` restores that file automatically when it finds no decisions in the
database. Nothing to remember, no command to run. The file is a version 2 document: decisions name
tracks by title / artist / album / album artist, each with a timestamp.

The file lives with the library rather than with Airdrome, so it backs up and travels with the
music it describes, and two libraries can't overwrite each other's decisions.
`DUPLICATES_FILEPATH` overrides the location.

**Upgrading an existing install — one time.** Older installs stored hand-made groups, and the mirror
was a version-less file of groups, which `dedup` refuses to restore. Follow *Recreating the database*
below: its step 0 converts the live groups from the old database with
`scripts/migrate_dedup_groups.py` (read-only; review the printed report of overlapping groups and what
the newest-wins rule made of them) before anything is dropped. Do not run `dedup-import` on the
old-format mirror itself — it refuses: its groups come from the old database, through the script.

### `airdrome dedup-import [path]`

Load decisions from *another* file — seeding a second library from the first, or reviving an old
backup. Idempotent; newest wins row by row. Accepts both formats: a version 2 file keeps its
timestamps, and an old version-less group file is converted with the same conversion rules as the migration script,
each group decided now in file order. What you import becomes this library's mirror; an import
that changes nothing leaves the mirror as it is. The library's own mirror in the old format is refused
(convert it with the script above, from the old database).

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

### `airdrome maint enrich`

Re-read every known file from disk: tags, MusicBrainz recording id, AcoustID id, md5, and the
chromaprint fingerprint (needs `fpcalc`). Unchanged files are skipped unless `--force`. Changes no
binding; prints how many files carry a recording id and a fingerprint.

- `--workers N` — parallel `fpcalc` processes (default 4)
- `--force`, `--dry-run`/`-n`

### `airdrome maint acoustid`

Look up fingerprinted files that have no recording id on AcoustID (needs `ACOUSTID_CLIENT_KEY`,
about 3 requests per second). A recording id is stored only when the best result scores ≥ 0.9 and
names exactly one recording; everything else is counted as ambiguous. Database only — nothing is
written into the files.

- `--limit N` — look up at most N files
- `--dry-run`/`-n` — the lookups still happen; nothing is saved

## MCP server

`airdrome mcp` lets an AI client (Claude Code) read your library and make point edits: the two
*wanted* lists (part 1 = tracks in your library with no file yet; part 2 = what you listened to but
have no file for, most played first), a track card, unmatched scrobbles with bind candidates and the
pipeline counts; edits are a *wanted*/*skip* verdict, *same*/*different* track, and binding or
rejecting scrobble aliases. Every edit commits at once and survives later `land` and `dedup` runs.
There are no library-wide actions.

The server listens on `127.0.0.1` only, and the secret token in its URL is the only access control:

```bash
# 1. Generate a token and put it in .env
python -c "import secrets;print(secrets.token_urlsafe(32))"   # → AIRDROME_MCP_TOKEN=<token> in .env

# 2. Run the server
uv run airdrome mcp

# 3. Register it with Claude Code, in your local (untracked) scope
claude mcp add --transport http --scope local airdrome http://127.0.0.1:8765/mcp/<token>
```

`airdrome mcp` never migrates. It refuses to start unless the database is at the current schema:
when it is merely behind, run `uv run alembic upgrade head`; when it carries a revision the scripts
no longer know (a database from before the migrations were squashed), recreate it — see
*Recreating the database* below. On start, and again before every edit, it restores your hand-made
manual dedup decisions from the mirror into empty tables (as `dedup` does), so no edit after a rebuild —
even one made while the server keeps running — can overwrite the mirror.

The server never prints or logs the token (its startup line shows `/mcp/<redacted>`, and the HTTP
access log is off). **Never put the URL in `.mcp.json`** — that file is tracked, and the token
would land in git.

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

### Recreating the database

There is no production database, so a schema change (or a database carrying a revision the scripts
no longer know) means recreating it rather than migrating. Everything in it is rebuilt from your
exports; the one hand-made part, your dedup decisions, comes back from the mirror.

```bash
# 0. Only if the mirror has no "version" (an install from before pair decisions): convert the
#    hand-made groups from the OLD database now — once it is dropped, nothing can read them.
uv run python scripts/migrate_dedup_groups.py ~/duplicates.v2.json   # read-only; review its report

# 1. Back up the decision mirror (the durable copy of your hand-made decisions)
cp "$LIBRARY_DIR/.airdrome/duplicates.json" ~/duplicates.json.bak   # or your DUPLICATES_FILEPATH

# 2. Dump the old database (so a conversion can be re-run if the rebuilt library leaves decisions
#    dead), then drop and recreate it (compose service `db`, the DB named in DB_DSN)
docker compose exec -T db pg_dump -U postgres -Fc airdrome > ~/airdrome-old.dump
docker compose exec db dropdb -U postgres airdrome
docker compose exec db createdb -U postgres airdrome

# 3. After step 0 only: put the converted file at the mirror path
mv ~/duplicates.v2.json "$LIBRARY_DIR/.airdrome/duplicates.json"

# 4. Rebuild it — the first command applies the schema
uv run airdrome import <path>...
uv run airdrome land
uv run airdrome dedup          # finds no decisions, restores them from the mirror, re-applies them
uv run airdrome status         # Dedup → Dead should be ~0
```

A non-zero *Dead* count means decisions name tracks the rebuilt library spells differently. Restore
the dump into a scratch database (`pg_restore`) and re-run the script against it (`--dsn`) to check.

If the server is running (or anything else holds a connection, such as the postgres MCP server in
`.mcp.json`), stop it first or `dropdb` refuses; `dropdb --force` closes those connections.
`airdrome mcp` also seeds the dedup decisions from the mirror into empty tables, on start and before
every edit, so running it before `dedup` cannot wipe the mirror.
