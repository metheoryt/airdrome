# CLAUDE.md

Guidance for AI coding agents working in this repository. These instructions OVERRIDE
default behavior — follow them exactly.

## How project knowledge is organized

Durable knowledge lives in git, split by the question it answers — not in a chronological
journal and not in out-of-tree auto-memory. When you settle something worth keeping, put it
where its *reader* will look:

| Doc | Reader | Answers |
|---|---|---|
| **README.md** | a user running the tool | install / configure / run; observable command behavior |
| **CLAUDE.md** (this file; `AGENTS.md` links to it) | an agent about to edit the code | where things live, load-bearing invariants, conventions, workflow |
| **[docs/design/](docs/design/)** | someone asking *why* | rationale, rejected alternatives, designed-but-unbuilt plans |
| **[ROADMAP.md](ROADMAP.md)** | planning next work | to-do ideas, open questions, agreed-but-unbuilt (summaries that link to docs/design/) |

Keep this file a **current-state map** — terse facts an editor needs in context. Push the *why*
(and the alternatives you rejected) into `docs/design/` and link to it; don't inline long
rationale here. Auto-memory is reserved for cross-project personal facts with no home in the repo.

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

End-to-end flow: `import <path>...` → `land` → `organize` → `dedup` → `sync all` →
`navi push`. Every write command is idempotent and takes `--dry-run`/`-n` (rolls back
instead of committing). Global `-v/--verbose` shows per-item detail (file picks, misses);
`-q/--quiet` suppresses non-essential output (both on the root callback, set via
`console.set_verbosity`). Run any command with `--help` for flags.

The top level is the pipeline plus `status` and `mcp`; three groups hold the rest — `sync` (playlist
reconcile across remotes), `navi` (stats destinations) and `maint` (housekeeping).

- **`status`** — read-only "where am I?" snapshot: config sanity (DB connectivity, `LIBRARY_DIR`
  state, Navidrome config + whether it's running) and per-stage counts (imported / landed /
  files / dedup / synced). Bypasses the root callback's session setup (early return keyed on the
  command name) and opens its own session, so it reports an unreachable DB instead of crashing —
  and never applies migrations. Lives in `terminal/status.py`.
- **`import <path>...`** — auto-detect each source and ingest its tracks/playlists/scrobbles.
  Per-source and dumb; pass many paths or a directory. `--as <source>` forces a source for
  every path; `--no-tracks`/`--no-playlists`/`--no-scrobbles` skip a kind. All importers
  resolve up front, so an unrecognized/ambiguous path fails before anything is written.
  **Music folders** (any directory; mp3/m4a/flac/ogg/opus/wav/aiff/wma) go in as one batch. An empty
  library gets the plain bulk scan (binding left to `land`); otherwise `classify` (`library/scan.py`)
  collapses repeats (md5, then fingerprint) and reports each recording as **new** / **have** /
  **upgrade** (kbps above the group's best file). Only new recordings and upgrades are imported,
  bound at once through the ladder. Probes of loose files are cached in `ScanCache` (path + size +
  mtime → md5, fingerprint, tag id), so skipped files are not re-fingerprinted.
- **`land`** — build the canonical graph from *everything imported* (run once, after all
  imports). Four numbered steps (printed via `console.step`): `do_unify` → `augment_aliases`
  → `match_aliases` → `copy_plays`. Step 1 binds files by the in-memory Apple path rebuild, then
  the identity ladder; its summary counts bindings per method. `-t/--threshold` (fuzzy alias
  match, default 0.4);
  `-m/--merge-playlists` (collapse same-name playlists, newest anchors); `--rebuild-playlists`
  (drop + rebuild canonical playlists from source, discarding backend-sync links).
- **`organize`** — copy (default) or `--move`/`-m` bound files into `LIBRARY_DIR`.
  `select_main` picks the best copy (bitrate, then container). (The `FileOrganizer`/
  `organize_library` engine still defaults to *move*; only the CLI defaults to copy — the
  safer, non-destructive user default.) The per-file "picking best" lines are verbose-only.
  `--dry-run` is threaded into `FileOrganizer` (not just the session rollback): `transfer`
  checks both preconditions, then returns the planned path without creating directories or
  calling `shutil` — so `library_path` and the count are the real ones, and the summary reads
  "would be moved/copied". The one thing it can't see is two tracks planning the *same*
  destination; that needs planned paths tracked across calls. **Upgrade swap** (the one narrow
  relocation): when a group's best file is not the one already placed in `Library/`, `swap_upgrade`
  moves the old main to the next free `Copies/` path and puts the upgrade at the freed `Library/`
  path. Nothing is overwritten: if the upgrade's transfer fails, the old main goes back unless the
  upgrade already landed (then the DB records the upgrade as main); a partial upgrade under a new
  extension is removed. Any other re-bind still relocates nothing (fs-reconcile #12). A swap that
  changes the extension reads to Navidrome as a remove + add — `navi push` after its rescan.
- **`dedup`** — batch rebuild of `Track.canon_id` from recording-identity groups
  (`normalize/dedup/recording.py`) + N flag-sets, layered with the manual pair decisions (see
  *Dedup decision mirror*; they win over automatic grouping both ways). Prints each group, then a summary
  with superseded / dead decision counts.
  `-s/--set` (repeatable, comma-separated fields; title always implicit), `-c/--canon`
  (`added`/`year`; the compilation rank leads `canon_order` for every group, so `-c` only breaks
  ties after it). With no `--set` the CLI uses `RECOMMENDED_SETS` (see *Dedup tuning notes*);
  the `auto_deduplicate` library function still falls back to a single all-fields set. There is
  no interactive review: manual decisions go through the MCP tools.
- **`dedup-import`** — manual entrance for a *foreign* or archived decision file (idempotent; newest
  wins row by row). Reads a v2 file as is and the old version-less group file by converting each group
  with the migration's conversion rules, decided at import time in file order — except the live mirror
  itself, which is refused in that format (it is converted from the old DB by the migration script). An
  import that changes no row does not rewrite the mirror. The library's own decisions need
  no command: they are mirrored to disk after every commit that touched one, and restored by `dedup`
  (and by the MCP server) into empty tables. See *Dedup decision mirror* below.
- **`sync <remote>` / `sync all`** — reconcile playlists across remotes (cloud sources +
  backends). One subcommand per remote (`apple_xml`, `apple_ms`, `navidrome`); `sync all`
  runs sources first, then backends. Fully non-interactive — hard conflicts auto-resolve to
  the last remote that edited the track. `-n`/`-y`. The Navidrome-stopped guard + WAL checkpoint
  fire only when a writable backend is in scope. See *Playlist reconcile* below.
- **`navi push`** — requires Navidrome stopped (probes `NAVIDROME_PORT`, prompts unless
  `-y/--yes`, then writes its SQLite DB directly). Pushes play counts + ratings for
  `NAVIDROME_USER`. (Playlists moved to `sync`.)
- **`maint renormalize`** — recompute `_norm` fields on tracks, aliases, and files (the escape
  hatch for a normalization-rule change, instead of a full reimport).
- **`maint enrich`** — re-enrich pass over existing `TrackFile`s (fs-reconcile #11): tags, ids,
  md5 from `absolute_path`, fingerprint via `fpcalc` (`--workers`). Skips unchanged files unless
  `--force`; a fingerprint is reset only when the bytes changed. Changes no binding.
- **`maint acoustid`** — fills `recording_mbid` (method `acoustid`) for fingerprinted files without
  one, only when the top AcoustID result scores ≥ 0.9 and names one recording. `--limit N`. Needs
  `ACOUSTID_CLIENT_KEY`; an invalid key aborts, any other failure is counted.
- **`mcp`** — serves the MCP tools (`mcpserver/`) over streamable HTTP on `127.0.0.1:AIRDROME_MCP_PORT`
  at `/mcp/<AIRDROME_MCP_TOKEN>`. Like `status` it bypasses the root callback (same early return) and
  **never migrates**: it exits 1 without a token, or off head (`migrations.schema_state()`: `behind` →
  "run `uv run alembic upgrade head`"; `unknown` revision, e.g. pre-squash → "recreate the DB", README
  *Recreating the database*, converting a version-less mirror with the migration script first). A
  mirror `seed_decisions` refuses is one red line, exit 1. Every edit snapshots the *whole* decision
  tables into the mirror, so an edit on rebuilt, empty tables would wipe the hand-made decisions from `duplicates.json`: each write tool
  (`write` in `server.py`) runs `restore_if_empty` first, in its own session under the write lock —
  which covers a DB rebuilt while the server runs — and startup runs it once too (`seed_decisions`,
  a data seed, not a migration) so reads see the decisions at once. Edit and verdict tools hold one
  module-level write lock (sync tools run in a threadpool; overlapping commits could write mirror
  snapshots out of order), and the server sets `mask_error_details=True`, so only `ToolError` messages
  reach the client. Same/different edits (`library/edits.py`) store one pair decision (`origin=mcp`), then replan
  the whole library (`plan_dedup` with the CLI's recommended sets) and apply the plan to the tracks the
  edit can reach (`edit_scope`), so the result equals the following `dedup` run; `is_main` is re-picked
  only for the touched roots (`recompute_main_files(s, root_ids)`). Only `EditError` refusals are an
  unknown id and `a == b`; a manual `same` crosses recording ids. It runs uvicorn
  on `server.http_app(path=...)` with `access_log=False` instead of `FastMCP.run`, because both the
  access log and FastMCP's own "Starting MCP server … on http://…/<path>" line print the path, and
  the path is the token. `test_server_logs_never_contain_token` guards this end to end.

## Architecture

### Data flow

1. **Import** (`ingest/`) — `import` auto-detects each source and writes raw
   `SourceTrack`/`SourcePlaylist`/`TrackFile` + `TrackAlias`/`TrackAliasScrobble` rows.
   Format parsers live under `cloud/` (Spotify, Last.fm, ListenBrainz, iTunes XML, Apple
   Media Services) plus a local folder scanner. `maint enrich` / `maint acoustid` add identity
   evidence to existing files.
2. **Land** (`library/unify.py`, `library/ladder.py`, `scrobbles/`) — unify source rows into
   canonical `Track`/`Playlist`, bind files (Apple path rebuild first, then the identity ladder
   for the rest — see *Identity ladder*), then augment aliases, bind them through the alias
   ladder, and copy scrobbles into `TrackPlay` history. Needs the full picture.
3. **Organize** (`library/organize.py`) — move/copy bound files into a structured directory.
4. **Deduplicate** (`normalize/dedup/`) — group duplicates by recording identity (shared
   recording id, close fingerprint) ahead of the flag-set buckets; twins link to a canon via
   `Track.canon_id`. Manual decisions (`PairDecision`, `CanonChoice`, keyed by track natural key) are
   resolved newest-first by `normalize/dedup/decisions.py` and layered over the automatic groups by
   `auto.plan_dedup`; see *Dedup decision mirror*. Rationale: [docs/design/dedup-decisions.md](docs/design/dedup-decisions.md).
5. **Sync** — push play counts + ratings into Navidrome's SQLite DB (`navidrome/`), and
   reconcile playlists across remotes (`playlists/`, see *Playlist reconcile*).

Off the pipeline, for the owner's AI client:

- **`wanted/`** — `verdicts.py` stores the owner's `wanted`/`skip` call (`WantedVerdict`, on a group's
  canon or per alias; `skip` beats `wanted`); `lists.py` builds the two paged wanted lists (library
  groups with no file; file-less played groups + unmatched scrobbled aliases).
- **`library/edits.py`, `library/card.py`** — manual point edits (`mark_same`/`mark_different` stored
  as pair decisions, `bind_aliases`/`reject_aliases`) and the track card / unmatched-alias candidates.
- **`library/search.py`** — `search_music`: whole-word match (last word a prefix, never substrings —
  `a-ha` normalizes to `aha`, inside `baha`) over track norms and over paths *relative to* each search
  root; a matching folder folds its subtree and names its subfolders with no audio (cover art left
  behind). Walked on every call, no cache but the word splitter's.
- **`mcpserver/`** — `build_server(engine)` wraps those as 12 FastMCP tools; each call runs in its own
  `tool_session`. A tool's docstring is what the AI reads, so semantics belong there.

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
- Folder scan writes only `TrackFile`; `do_unify` resolves orphan `TrackFile`s through the
  ladder (no `SourceTrack(FOLDER)` layer, no FOLDER enum value). `read_tags` (`library/tags.py`)
  decodes mutagen's bytes values (MP4 freeform, ID3 UFID) explicitly — `str(bytes)` breaks id
  equality across containers.
- Spotify plays under 30 seconds are excluded.

### Session lifecycle

Sessions are created centrally in the root `app` callback (`terminal/app.py`) and stored as
`AppState(session, dry_run)` on `ctx.obj`; a close hook commits, or rolls back under
`--dry-run`. `upgrade_to_head()` runs inside the callback (not at import), so a bare `airdrome`
/ `airdrome --help` and `status` skip it — but `airdrome <sub> --help` passes through the callback
and connects to `DB_DSN` and migrates (an `alembic` crash against the live DB, 2026-10-17). <!-- src: metheoryt/airdrome sleep 2026-10-17 g15 --> `expire_on_commit=False` keeps ORM objects populated after a commit, so
code (and MCP tools building their answer) can read them without a reload.

**When adding a CLI command:** take `ctx: typer.Context` first, read `state: AppState =
ctx.obj`, set `state.dry_run = dry_run` if exposed, pass `state.session` to business logic.
Business logic takes `s: Session` and uses `s.flush()` for intermediate persistence — it
must **not** call `s.commit()`. Intentional exception: `reconcile` (`sync`) commits once per playlist, right
after the backend committed its half, so it takes a behavioral `dry_run` (flush instead), and
`NavidromeAdapter(dry_run=True)` discards its own Navidrome commits; `navi push` has no `--dry-run`.
`test_cli_dry_run.py` runs every write command with and without `-n` on the real callback. The MCP server is the
second session owner outside the callback: `mcpserver.session.tool_session` opens one session per
tool call (with the dedup mirror installed), commits on success, rolls back on any exception — so a
tool call is one transaction, and the domain functions it wraps still only flush.

**A rollback only undoes the DB.** A command that also writes outside the session — files,
an external backend — must take `dry_run` as a *behavioral* argument and skip the side effect
itself; `state.dry_run` alone is not enough (see `organize`, and `sync navidrome`).
The other way to satisfy the rule is to hang the side effect off the commit itself, so a
rollback never reaches it — what the dedup decision mirror does.

### Key models (`models.py`)

- **`Track`** — canonical track; normalized `_norm` title/artist/album fields; unique on
  (title, artist, album, album_artist) with `NULLS NOT DISTINCT`, so that raw tuple names at most one
  track (it is what a decision's key is). Self-referential `canon_id` links a twin to its
  canon — and is *terminal* (a canon is never itself a twin; no chains), so readers resolve
  with a single hop. `canon_folded` marks a twin outside its canon's *core* (joined only through a
  manual decision); `TrackGroup.core_members` / `main_candidates` read it (see *Navidrome integration*).
- **`PairDecision`** — a manual `same` / `different` about two tracks, each named by its natural-key
  JSON text (`key_a < key_b`, unique per unordered pair), with `decided_at` and `origin`
  (`mcp` | `import` | `migrated`). **`CanonChoice`** — a manual pick of a track as its group's main
  version (unique per track). No FKs to `Track`: a decision outlives the track row and a rebuilt DB.
- **`TrackFile`** — audio file on disk with Mutagen-extracted metadata; `is_main` flags the
  best-bitrate copy; `library_path` is the organized destination; `enrich()` reads tags, ids and
  md5 from `absolute_path` into the row (no fingerprint — `fingerprint_files` adds it).
- **`TrackAlias`** — one scrobble-source entry per unique title+artist+album+platform.
- **`TrackAliasScrobble`** — an individual play event; `copy_plays` materializes matched
  aliases into canonical **`TrackPlay`** history.

### Normalization & matching (`normalize/`, `match.py`)

All text fields have `_norm` variants (lowercased, accents stripped). Fuzzy dedup and alias
matching use PostgreSQL trigram similarity (`pg_trgm`), weighted artist/album_artist 75%,
album 25%. Threshold defaults to 0.4.

### Identity ladder (`library/ladder.py`, `library/fingerprint.py`, `normalize/dedup/recording.py`)

Rationale, calibration and acceptance numbers:
[docs/design/identity-ladder.md](docs/design/identity-ladder.md).

- **Rung order.** File → track: Apple path rebuild first (in memory, `PathTailIndex`; beats the
  file's tags), then `resolve_file`: recording id → fingerprint → tags, a new `Track` from tags
  last. Alias → track: player-submitted id → exact tags → trigram fuzzy; the ListenBrainz-mapped id
  only breaks ties, never binds or groups. Every binding records `BindMethod` + confidence
  (`TrackFile.bind_*`, `TrackAlias.match_*`, `Track.canon_*`).
- **`Track` rows are never re-keyed or merged.** Recording identity groups tracks as twins, which
  keeps the natural keys (title, artist, album, album artist) and the pair decisions that name them valid.
- **One recording id per group.** A track's id is the one its files agree on (`recording_of_track`);
  disagreeing files leave it with none (`dedup` reports it as conflicted). Nothing binds or links
  across two ids. A manual `different` is a hard, transitive cannot-link in `dedup` — unless a newer `same` joined the
  two (newest wins). A manual `same` is the one thing that crosses recording ids.
- **The fingerprint representative** of a track is its highest-bitrate fingerprinted file, never
  `is_main` (which `dedup` itself rewrites), so `dedup` is idempotent.
- **Fingerprints:** stored as signed int32 (mask `0xFFFFFFFF` before comparing). `FP_MATCH = 0.89`;
  a match links only when durations differ by ≤ 2 s (`FP_MAX_DURATION_GAP`), else it is a review
  candidate.
- **A rejected alias is `track_id NULL` + `match_method = MANUAL`** (`reject_aliases`): unbound for
  good, its `TrackPlay` rows deleted, and matching skips it. An unbound alias with any other
  `match_method` is merely unmatched. Never encode a reject as `alias.track = None` —
  `Track.aliases` is delete-orphan, so that deletes the alias.
- **`ACOUSTID_CLIENT_KEY` and `AIRDROME_MCP_TOKEN` are `SecretStr`s** and every Typer app sets
  `pretty_exceptions_show_locals=False`; never print either.

### Dedup decision mirror (`normalize/dedup/decisions.py`, `store.py`, `mirror.py`)

Why pairs, and the rejected alternatives: [docs/design/dedup-decisions.md](docs/design/dedup-decisions.md).

Manual decisions are hand-made, but the Postgres DB is disposable (recreated on schema change). So
the DB is the working copy and `LIBRARY_DIR/.airdrome/duplicates.json` is the durable mirror —
one-directional, DB → file. Colocating it with the library makes it per-library by construction;
`DUPLICATES_FILEPATH` overrides the path.

- **Resolution** (`decisions.resolve`, pure): pairs apply newest first; at an equal `decided_at` every
  `same` before any `different`, then by key order, so the result never depends on row order. A
  `same` joins two components unless an accepted (newer) `different` separates them; a `different`
  separates unless a newer `same` already joined them; the loser is *superseded*. An end naming no live
  track makes the decision *dead*: ignored, kept, counted (`status`, `auto_deduplicate`). A group's
  canon is the newest `CanonChoice` among its members, else `canon_order`.
- **Format v2:** `{"version": 2, "pairs": [{a, b, kind, decided_at, origin}], "canons": [{track,
  decided_at, origin}]}`, keys as `[title, artist, album, album_artist]` lists, deterministic order.
- **Upsert rule, everywhere:** a row replaces an existing one on the same pair / track only if strictly
  newer; so a v2 import keeps the file's timestamps and newest wins row by row. `import_decisions`
  rejects a pair or canon listed twice and clamps a future `decided_at` to now. An MCP edit that lands
  at or before the stored row's time is placed 1 µs after it (the edit is the newest by definition).
- **Snapshot and write are split on purpose.** `stash_mirror_snapshot` (in `store.py`) exports the
  whole tables inside the writing transaction, where pending changes are visible, and parks it in
  `session.info`. The `after_commit` listener that `install_mirror` attaches (wired once, in the root
  callback and the MCP `tool_session`) only serializes that dict. Exporting *in* the listener would
  emit SQL with no transaction open; writing *before* the commit would let a rollback leave the file
  ahead of the DB. An `after_soft_rollback` listener drops an unlanded snapshot.
- **Always the whole tables, never a delta** — nothing but the tables is authoritative.
- **An empty export writes an empty v2 document, it is not skipped.** Load-bearing: a stale file left
  behind by a deliberate full clear would re-seed the next empty DB.
- **`restore_if_empty` seeds only empty tables**, so it never fights the DB. It runs from
  `terminal/pipeline.py` (and the MCP server), not inside `auto_deduplicate`, so `settings` stays out of
  the domain layer. A file that fails to parse raises, and so does an old version-less group file: it
  is not restored (importing nothing would let the next commit overwrite it with an empty v2 document).
- **Migrating an existing install** (once): `uv run python scripts/migrate_dedup_groups.py OUT` reads
  the live `dedupgroup` tables (before the schema change; `normalize/dedup/legacy.py` holds the
  conversion) and writes a v2 file plus a report of the overlapping clusters. Review it, `pg_dump` the
  old DB (always, not a plain drop: the conversion can then be re-run), recreate the DB, move the v2 file to
  the mirror path, `import` → `land`; `dedup` then restores it and `status` should show ~0 dead
  decisions. README *Recreating the database* says so too. The `restore_if_empty` refusal points
  there; `dedup-import` refuses the live mirror in the old format, since importing it onto itself
  drops (before `land`) or reorders (after it) the decisions.

### Navidrome integration (`navidrome/`)

`navidrome/models.py` maps Navidrome's SQLite schema read-only; sync writes `Annotation`
(play counts, ratings) per user and checkpoints WAL before writes. Track sync is
dedup-group-aware: exactly one `is_main` file exists per group (owned by whichever member
has the best copy — often a twin, not the canon), but plays are **summed** and rating/loved
**aggregated** across the whole group, so plays on other members aren't dropped. Both writers
keep the one-main invariant — `organize` picks it across canon+twins, and
`recompute_main_files` re-picks it whenever the canon graph changes (library-wide after `dedup`; scoped
to the touched roots after a point edit). **Core rule:** in a manually folded group the main comes from
the canon's *core* (the canon plus the twins automatic grouping alone joins to it; `canon_folded` False
on them), so a folded-in higher-bitrate file never becomes main; the whole group is the fallback only
when the core has no file (`TrackGroup.main_candidates`). `organize`'s upgrade swap and import
`classify`'s "upgrade" bar use the same candidates; when the main is already placed, `organize` puts
only manually folded files into `Copies/` (unless an upgrade swap runs, where every unplaced file goes
there). Selection only flips the
flag; the next `organize` relocates only for the upgrade swap (a better copy displacing the
placed main), and any other relocation is the reconcile roadmap.

### Playlist reconcile (`playlists/`)

Why this shape (hub model) + rejected alternatives:
[docs/design/playlist-reconcile.md](docs/design/playlist-reconcile.md).

Airdrome is the source-of-truth **hub**; every peer is a `PlaylistAdapter` (`adapter.py`)
*remote* reconciled against a per-`(playlist, remote)` **base** (`PlaylistLink.synced_track_ids`).
`writable=False` marks **read-only** remotes — `SourcePlaylistRemote` (Apple `SourcePlaylist`
rows), pull-only, never written; backends are read-write (`NavidromeAdapter`).

- **Engine** (`sync.py`): `_sync_pair` does a multiset 3-way merge (`base`/`ours`/`theirs`)
  for one (playlist, remote). Read-only remotes apply only `theirs→ours` and store `theirs`
  as the new base (not the merged list) — that is what stops a downstream-deleted or
  local-only track from being resurrected.
- **Orchestrator** (`orchestrator.py`): `reconcile(s, adapters)` runs every playlist against
  the remotes in order, in one pass and without prompting. It gathers each remote's
  base/membership; `detect_conflicts` (`conflicts.py`) finds order-dependent (add-vs-remove)
  edits. A clean playlist auto-merges pairwise. A conflicted one goes through
  `resolve_latest`: fold every remote in order, then override *only* the conflicted tracks
  with the multiplicity of the **last remote that actually edited each** (`verdicts()` — a
  remote whose `theirs` count equals its own base abstains rather than winning by position).
  The result is forced outward by priming each base to `theirs`, so the same `_sync_pair`
  propagates it. Each auto-resolution prints the track and the remote that won it.
- **`land` seeds, `sync` updates**: `unify_source_playlists` only writes membership when it
  *creates* a canonical playlist; existing playlists are owned by `sync`. Re-importing a
  snapshot never re-unions, so a downstream delete sticks.

### Migrations

No production DB — recreate on schema change. That is a habit of the unreleased, in-development
phase, not a design constraint: new manual decisions live in the DB only (the dedup mirror is a
stopgap until recreation stops). <!-- src: metheoryt/airdrome sleep 2026-10-17 g15 --> All prior migrations are squashed into one
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
| `DUPLICATES_FILEPATH` | `LIBRARY_DIR/.airdrome/duplicates.json` | Override for the dedup decision mirror (`settings.duplicates_file` resolves it) |
| `SEARCH_DIRS` | `[]` | Music folders `search_music` walks besides `LIBRARY_DIR` (JSON list; `settings.search_roots`) |
| `NAVIDROME_DB_DSN` | `None` | Path to Navidrome's SQLite DB |
| `NAVIDROME_USER` | `None` | Navidrome user that owns synced play counts/ratings |
| `NAVIDROME_PORT` | `4533` | Port the `navidrome` commands probe to confirm the server is stopped |
| `ACOUSTID_CLIENT_KEY` | `None` | AcoustID *application* key for `maint acoustid` (never print it) |
| `AIRDROME_MCP_TOKEN` | `None` | Secret path segment of the MCP server URL; `airdrome mcp` refuses without it (never print or log it) |
| `AIRDROME_MCP_PORT` | `8765` | Localhost port `airdrome mcp` listens on |

`/data/`, `/navi/`, `/dist/`, `.env` are gitignored. Treat `data/` as local runtime state the
user backs up separately — don't optimize its serialization for "cleaner git diffs"; only
round-trip correctness matters.

## Workflow

- Solo project, no PRs. Work directly on `main` by default. Branch from `main` only when the
  task is serious/risky or there is already a lot of uncommitted work in progress; when done and
  confirmed, **squash-merge** the branch back into `main` and delete it.
- **Check the current branch before committing** (`git branch --show-current` / `git status`) —
  don't assume the branch from earlier in the session; a merge between turns can land you back on
  `main` unexpectedly.
- Always `git push` after committing/merging to `main`; **never force-push**.
- Cover changes with meaningful tests; lean on pytest fixtures heavily.
- Give every function and method a one-line docstring.
- Comment non-obvious code, explaining the decision — the more complicated, the more explicit.
- Call out places that can be simplified as you go.

## Conventions

- Line length 110 (Ruff), target `py314`, rules `E,F,I,W,UP,B,SIM,C4,PIE,RUF,ANN`.
- Imports: first-party `airdrome` group separated; `factories` known-local; two blank lines
  after the imports block.
- All datetimes are timezone-aware UTC.
- Use `get_or_create()` for idempotency; re-running commands is safe.
- `typer.Argument`/`typer.Option` sentinels as defaults are intended (exempt from B008);
  optional no-op ingest/sync hooks are exempt from B027.
- Ruff's select list also carries **`ANN`** (annotation presence) — gortex's native Python
  resolver keys off declared types, so a missing param/return annotation is a lost graph edge,
  not just style. `ANN` is ignored wholesale for `tests/*`, `alembic/versions/*.py` and
  `alembic/env.py`; `ANN401` (bare `Any`) is scoped to `models.py` and `cloud/sources.py`, the
  two genuinely dynamic boundaries — anywhere else a bare `Any` is an error.
  <!-- src: airdrome e0daf7a | 2026-09-12 -->
- Type checking is **`ty`** (`uvx ty check`), configured in `pyproject.toml`.
  `[tool.ty.environment]` pins it to `.venv` / 3.14 — without that every first-party and
  third-party import degrades to unresolved and the diagnostics are noise — and
  `[tool.ty.src]` covers `airdrome`, `tests`, `alembic`. It is in neither pre-commit nor the
  pytest gate, so run it by hand; it carries a standing nonzero diagnostic count (mostly
  pre-existing `| None` narrowing gaps), which is a baseline to compare against rather than a
  clean bar to hold.
  <!-- src: airdrome e0daf7a | 2026-09-12 -->

## Dedup tuning notes

- Recommended `dedup` sets (precise yet mass): `-s "artist,duration" -s
  "artist,year" -s "album_artist,duration"` — reaches ~98% of the reckless `artist`-only
  ceiling while gating every merge on a matching duration OR year, so edits/remixes/live cuts
  don't collapse. These are codified as `RECOMMENDED_SETS` (`normalize/dedup/grouping.py`) and
  are the no-`--set` CLI default.
- Bare `-s "artist"` is dangerous (no discriminator). "All fields" only finds exact-dup files.
- `compute_auto_dedup_groups` skips a track when *all* selected non-title key fields are blank.
- `loved` is deliberately out of canon ordering — a group's loved status is derived from the
  whole group, so it must not decide which member is canon. The merge re-query must use the
  same `canon_order(strategy)` or single vs. merged groups disagree on `group[0]`.

## Roadmap

[ROADMAP.md](ROADMAP.md) tracks forward-looking work (the MCP server with `wanted` and point edits —
designed 2026-10-18, see `docs/superpowers/specs/2026-10-18-airdrome-mcp-design.md` — playlist
management, a Telegram management bot, the filesystem ⇄ Airdrome reconcile pipeline). Read it at the
start of any non-trivial task.

**When a new idea surfaces** in a dialog — a feature, a "we should eventually…", or a design
we settle on but won't build now — **ask the user whether to add it to ROADMAP.md**, and
record it there if they agree. When an item ships, move its current-state into this file (and
mark its `docs/design/` write-up built), then remove it from the roadmap.
