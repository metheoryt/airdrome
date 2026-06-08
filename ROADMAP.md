# Airdrome Roadmap

The single place to track to-do ideas, open design questions, and agreed-but-unbuilt
work. Code-level "how it works today" lives in [AGENTS.md](AGENTS.md); this file is for
what we *want to do next*.

**For agents:** skim this file at the start of any non-trivial task so suggestions line
up with the plan. When a new idea surfaces in conversation — a feature, a "we should
eventually…", a design we settle on but won't build yet — **ask whether to add it here**,
and if yes, drop it under the right section with a status marker. When an item ships,
fold the durable design into AGENTS.md and remove it here.

Status legend: 💡 idea (unscoped) · 🧭 designed (settled, not built) · 🔨 in progress ·
🅿️ parked. Done items are deleted, not checked off — git history is the archive.

---

## Now

The immediate, next-up work.

- 🧭 **Auto-managed dedup JSON (drop manual export/import).** Confirmed dedup groups
  (`dedupgroup`/`dedupgroupmember`) are real human work, but the Postgres DB is disposable
  (recreated on schema change), so today they survive a rebuild only if you remember to run
  `dedup-export` before and `dedup-import` after. Replace the two manual commands with
  automatic persistence: mirror confirmed groups to a JSON file whenever they change (TUI
  confirm), and restore from it into an empty DB on startup. One-directional (DB = working
  copy, JSON = durable mirror); atomic write (temp file + rename) to survive a crash.
  **Colocate the file with the library** — `LIBRARY_DIR/.airdrome/duplicates.json` — so it's
  per-library by construction (no two-library clobber), travels and backs up *with* the
  library, and needs no library→file mapping. `DUPLICATES_FILEPATH` stays as an override.
  Keep `dedup-export`/`dedup-import` until this lands — otherwise a schema rebuild silently
  loses canons. Leans into the self-repairing reconcile direction below.

- 💡 **Merge specified playlists.** Some playlists are different versions of the same
  list under slightly different names. Need a way to point at two (or more) playlists by
  name and merge them into one. Falls under *Playlist management* below — this is the
  first concrete slice of it. Open questions: which name/identity survives, dedup of
  members on merge, whether it's a new CLI verb (`library playlists merge <a> <b>...`)
  or part of a broader playlist toolset.

- 💡 **`organize --dry-run` isn't actually dry.** `FileOrganizer.transfer` runs
  `shutil.move`/`shutil.copy` unconditionally; `--dry-run` only rolls back the DB
  (`library_path` writes), so files are *already relocated on disk* when the rollback
  happens. The CLI summary also still says "moved/copied" under dry-run. Default-copy
  softens it (originals survive a dry-run move… because nothing moved), but it's still
  misleading. Fix: gate the filesystem op on `dry_run` and have the summary say "would
  copy/move N" — needs `dry_run` threaded into `organize_library`/`FileOrganizer`. Ties
  into the self-repairing organize state machine in the reconcile design below; do it
  there or as a standalone correctness fix first.

---

## Playlist management

Navidrome is a player, not a library manager, and playlists are the one entity Airdrome
can't shape indirectly through file tags (unlike track metadata). So playlists need a
first-class management story of their own. **Built (2026-06-08):** `airdrome sync` reconciles
playlists across remotes — Airdrome as hub, every peer a remote with a per-`(playlist, remote)`
base, interactive resolver on conflicts. Design now lives in AGENTS.md *Playlist reconcile*.
`land --rebuild-playlists` still nukes and rebuilds from source.

- 🅿️ **Parked (own discussion): extend the hub/remotes/base model to tracks** (metadata,
  ratings, loved, play history reconciled per-remote against a base). Same engine as the
  playlist reconcile, richer conflict surface (which *field* wins, not just membership). Play
  counts already flow one-way via `navi push` stats; the general version makes that base-aware.

### Complementary editing tools (independent of the above)

- 💡 **m3u round-trip.** Export resolved playlists as `.m3u`, edit in any external tool,
  re-import. Pro: zero bespoke UI. Con: needs stable file-path ↔ Track resolution on
  re-import, and on-disk paths must match the organized library.
- 💡 **A small subset of playlist tools in the CLI.** Merge (the *Now* item), rename,
  dedup members, split, reorder. Keeps everything in the canonical model.

---

## Telegram bot

A bot to manage the Airdrome library from the phone. Built incrementally, one feature at
a time.

### Feature 1 — "Upload a file for a track" 💡

Goal: fill in missing audio files for tracks that have play history but no file on disk
(the long tail of "I listened to this a lot but never had the file").

Flow:

1. User uploads a music file to the bot.
2. Bot downloads it, analyzes it (Mutagen tags, same path as `TrackFile.enrich()` /
   the folder scanner), and verifies/updates the file's tags.
3. System searches for a matching `Track`. A match can also be **pre-selected** before
   upload — the user picks the target track first, then sends the file for it.
4. Track discovery in the bot, two ways:
   - **Search** by title/artist.
   - **Browse handy lists**, e.g. *"top listens without any file"* — high-play-count
     tracks that have no bound `TrackFile`. (Feasible against current models: `Track`
     play history via `TrackPlay`/aliases, file presence via `TrackFile`.)
5. If the track **already has a file**, the bot sends the existing file back with its
   metadata and prompts for what to do with the *uploaded* one:
   - **Delete** the upload,
   - **Move to Copies** (ties into the `copies_dir` concept from the reconcile design), or
   - **Leave** — keep the existing file, discard the upload.

Open questions: where uploaded files land before binding (a staging/watch folder?
overlaps with the reconcile `watch` design), how tag verification decides accept vs.
correct, auth (single-user — lock to one Telegram user id), and how this rides on the
not-yet-built `ingest_one()` per-file pipeline.

Later bot features: TBD — capture them here as they come up.

---

## Filesystem ⇄ Airdrome reconcile (🧭 designed, not built)

A settled design for self-repairing organize, a watch folder, and a tag `reconcile`
pipeline. Core mental model is three hops; organize is only the last:
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
This pipeline underpins the Telegram bot's upload feature — the bot is one front-end to
`ingest_one()`.
