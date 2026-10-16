# Airdrome Roadmap

The single place to track to-do ideas, open design questions, and agreed-but-unbuilt
work. Code-level "how it works today" lives in [CLAUDE.md](CLAUDE.md); this file is for
what we *want to do next*.

**For agents:** skim this file at the start of any non-trivial task so suggestions line
up with the plan. When a new idea surfaces in conversation — a feature, a "we should
eventually…", a design we settle on but won't build yet — **ask whether to add it here**,
and if yes, drop it under the right section with a status marker. When an item ships,
fold the durable design into CLAUDE.md and remove it here.

Status legend: 💡 idea (unscoped) · 🧭 designed (settled, not built) · 🔨 in progress ·
🅿️ parked. Done items are deleted, not checked off — git history is the archive.

---

## Now

The immediate, next-up work.

- 🧭 **Identity ladder + fingerprints** — first item of the *Identity & acquisition* programme
  below; decided 2026-10-16, to be built in a fresh session. Lands before `wanted` so CD rips
  and untagged files are not counted as ghosts.

- 🧭 **Playlist editing tools (`merge` + `dedup-members`).** A new `playlists` command
  group: `merge <base> <other>...` folds human-specified near-duplicate playlists into one
  (tombstone table keeps it durable across re-`land`); `merge --same-name` auto-groups by
  name (newest anchors), replacing `land --merge-playlists`; `dedup-members [<name>...]`
  collapses canon-duplicate member rows. Pure canonical-hub edits — `sync` carries them out.
  Full design + the data that scoped it:
  [docs/design/playlist-tools.md](docs/design/playlist-tools.md).
  - **Migration the design predates: the merges already in the DB are recorded nowhere.**
    `merge_by_name=True` folds an absorbed source playlist in *without* giving it a canonical
    of its own — its `SourcePlaylist` row survives untouched and the surviving canonical keeps
    only the newest `source_id`. That is stable while the flag stays (a second
    `--merge-playlists` run skips the absorbed names via the `preexisting_ids` guard, creating
    nothing), but the first `land` *without* it runs `_unify_per_source`, which keys on
    `(platform, source_id)` and mints a fresh canonical for every absorbed source — silently
    taking the merges apart. The 2026-09-09 rebuild is in exactly this state: 317 canonicals
    from 766 source playlists, every collapse implicit. So removing the flag (decision #2) has
    to backfill a `playlist_merge` tombstone for each source playlist that currently holds no
    canonical of its own, in the same change.

- 🧭 **Batch the file-binding pass in `land`.** `_bind_track_files` asks the database one
  `ILIKE '%…%'` question per candidate path, and `possible_locations(max_suffix=2)` yields
  12–24 candidates per Apple source track — so a real run (27k source tracks, 7k files)
  issues ~490k full scans of `trackfile` at 8–11 ms each. Measured 2026-09-09 against the
  live library: **~40 minutes for step 1/4 alone**, DB-bound at 100% CPU with no progress
  output — the "N+1 storm that looks like a freeze" the `terminal/app.py` comment warns of.
  **An index is not the fix.** A forced `pg_trgm` GIN lookup measures 0.28 ms against the
  scan's 8–11 ms, but at ~7k rows the planner costs the seq scan *cheaper* (215 vs 256) and
  ignores the index — so adding one changes nothing at this library size and only starts
  paying off at some larger, unmeasured one. The fix is to stop asking the database: load
  `source_path` for the unbound files once, and match candidates against an in-memory index
  of path tails. A dict/suffix lookup is what that ILIKE actually expresses, so it can
  preserve the semantics the comment there documents — case-insensitive matching, and
  `autoescape` keeping `_`/`%` literal (paths sanitize `/` to `_`, e.g. "AC_DC"). Keep both
  current behaviours: one `rel_path` may hit several files (dedup by id), and only files
  with `track_id is None` bind. Should turn ~40 min into seconds.
  - Step 3/4 (alias→track matching) took **~48 minutes** in the same run. Unprofiled — it
    may be the same shape of problem or a different one. Measure before assuming.

---

## Identity & acquisition (architecture review, 2026-10-16)

The review's one finding: the three places that still feel off — iTunes-shaped file binding,
the playlist 3-way merge, the size of dedup — share a cause. **Track identity is built from
strings and paths, and every subsystem patches that its own way** (`ILIKE` on a rebuilt Apple
path, weighted `pg_trgm`, the exact 4-tuple unique key, flag-set buckets). The items below are
one programme. **Decided 2026-10-16 (build order):** (1) identity ladder + fingerprints,
(2) `wanted`, (3) Spotify as a live remote. The 🧭 items are settled; 💡 ones still want their own
discussion. Build (1) in a fresh session.

Live numbers that scoped it (2026-10-16): 13,243 canonical tracks after dedup, **6,879 of them
with no file anywhere in their group**; 6,986 files, all bound; 202,584 plays; 26,745 / 79,051
aliases matched. The file-less half is mostly streaming pass-through (tracks played once or
twice and never added), but it also hides unmatched duplicates of tracks that *were* listened
to, and the odd gem that never reached the library — so it is classified, never cut by play
count alone.

- 🧭 **Identity ladder.** One `resolve(claims) -> Track` with ordered matchers — MusicBrainz
  recording id from tags → acoustic fingerprint (chromaprint / `fpcalc`) → normalized tags →
  the Apple path rebuild as an *opt-in last* matcher. Every binding (file→track, alias→track,
  twin→canon) records `method` + `confidence`, so a better matcher can later upgrade a weaker
  binding and a wrong match can be audited. This is what makes `import <any directory>` work:
  scan → enrich (tags, MBID, fingerprint, `content_hash`) → resolve, no iTunes layout assumed.
  Also widens `MusicScanner.EXTENSIONS` (ogg/opus/wav/aiff/wma) and stops `organize` mirroring
  Apple's layout as the only option.
  - Dedup shrinks rather than disappears: with recording identity most twins collapse at `land`,
    and what is left is "several files of one recording → pick the best" (`select_main_file`
    already does that) plus *file-less* duplicates among the ghosts, which only metadata can
    group — so the manual review path and the 881 hand-made groups stay.
- 🧭 **Fingerprint-first ingest, no Picard prerequisite.** Picard tagging of the library is in
  progress and the CD/DVD rips are untagged; tagging them by hand first is the thing that
  frustrates. Airdrome can do what Picard does underneath: `fpcalc` → AcoustID lookup → MBID
  (free API key, ~3 req/s, ~40 min for 7k files), or compare fingerprints locally without the
  lookup. **Decided: both modes** — the fingerprint is computed once and stored on `TrackFile`;
  local comparison answers "do I already have this recording" offline, the AcoustID lookup (he
  has an account) is a separate command that adds the MBID. Then "point it at the CD rips and tell me which recordings I don't already have" is
  one command, and Picard becomes optional polish afterwards, not a gate.
- 💡 **`wanted` — the file-less tracks as a first-class list, classified.** Signals, not a play
  threshold: plays spread over months vs. one session, membership in any playlist, loved /
  rated, present in a *library* source (Apple XML / Spotify saved) vs. streaming-history only,
  and an unresolved near-duplicate of a track that has a file (run the ghost dedup first).
  Reasons `missing` and `upgrade` (bitrate under a cutoff). Output feeds the acquisition loop
  below and the Telegram bot's upload feature.
- 💡 **Acquisition loop over Soulseek.** `wanted` → CSV for `slsk-batchdl`, or `slskd`'s REST
  API directly → drop folder → `ingest_one` (fs-reconcile layer 3) → fingerprint confirms it is
  the wanted recording. Soulseek is the right network for single tracks; torrents are not.
  **Avoid Lidarr**: its metadata server was down or rate-limited through 2025–2026 (issue
  #5498, reopened 2026-01). Reference designs: Soularr (`cutoff_unmet` = upgrades),
  DroppedNeedle v2 (2026-07, Lidarr-free wanted→slskd→tag→organize→auto-upgrade).
- 🧭 **Spotify as a live read-write remote, both directions.** The editing picture (2026-10-16): Spotify / Apple
  Music stay in daily use — loved tracks and playlists change there — *and* Navidrome is where
  the offline collection is played and its playlists edited. Two live editors, so the
  per-remote base + 3-way merge is confirmed as the right engine, not complexity to cut. What
  changes: Spotify has a Web API, so it can be a `writable` remote polled live instead of a
  snapshot import. **Decided: bidirectional** (`writable=True`) — a track added in Navidrome
  reaches the Spotify playlist when Spotify has it. Apple Music is being dropped from daily use,
  so it stays an export-only snapshot — an important one, but a snapshot. Tracks
  only Navidrome has stay Airdrome/Navidrome-only — the engine already keeps a remote's
  unrepresentable tracks out of that remote's base. Loved / ratings then want the parked
  "extend the hub model to tracks" item.
- 💡 **Scrobbles going forward: multi-scrobbler → ListenBrainz; Airdrome backfills history only.**
  multi-scrobbler forwards live plays from Spotify, Apple Music and Subsonic/Navidrome (~33
  sources). Reinforces the Last.fm/Spotify importer sunset under *Ingest sources*.
- 💡 **Open: PostgreSQL → SQLite + in-memory fuzzy (rapidfuzz).** Postgres is required only for
  `pg_trgm`, and the 40-minute `land` shows the indexes are not carrying the load at 16k tracks.
  One file, no Docker, is the beets-like feel. Decide after the identity ladder lands — it
  changes how much fuzzy matching is left.
- 🅿️ **beets as the library layer** (import / organize / `chroma` fingerprints / `duplicates`
  with `duplicate_action: upgrade`, 2.14) with Airdrome reduced to history + playlists +
  Navidrome writes. Would halve Airdrome but beets wants to own the layout. Revisit only if the
  library half still feels heavy after the identity ladder.
- Reference points from the 2026-10 survey: Navidrome 0.64 (plugins since 0.61, faster m3u
  import, smart playlists still beta, no play-count import — the direct SQLite write stays);
  NaviSync (Last.fm → Navidrome plays), NaviSpot / navify (Spotify playlists → Subsonic with a
  missing-track report); Picard 3.0 (`picard-cli`, plugin API). No project found with a
  per-remote base for playlist reconcile — that part is Airdrome's own.

---

## Playlist management

Navidrome is a player, not a library manager, and playlists are the one entity Airdrome
can't shape indirectly through file tags (unlike track metadata). So playlists need a
first-class management story of their own. **Built (2026-06-08):** `airdrome sync` reconciles
playlists across remotes — Airdrome as hub, every peer a remote with a per-`(playlist, remote)`
base, and conflicts auto-resolved per track by the last remote that edited it. "How it works
today" lives in CLAUDE.md *Playlist reconcile*; the long-form rationale + rejected alternatives are in
[docs/design/playlist-reconcile.md](docs/design/playlist-reconcile.md).
`land --rebuild-playlists` still nukes and rebuilds from source.

Where playlists are edited (2026-10-16): Spotify / Apple Music *and* Navidrome, both live — see
*Identity & acquisition* for what that settles (the 3-way engine stays; Spotify becomes a live
writable remote).

- 💡 **Backend orphan cleanup after merge.** When `playlists merge` absorbs a playlist, its
  Navidrome counterpart is left as a stale orphan (its `PlaylistLink` is gone, so `sync` neither
  updates nor deletes it). Deleting backend playlists that no longer map to any canonical is its own
  concern with its own risk surface. See [docs/design/playlist-tools.md](docs/design/playlist-tools.md)
  *Follow-up*.

- 🅿️ **Parked (own discussion): extend the hub/remotes/base model to tracks** (metadata,
  ratings, loved, play history reconciled per-remote against a base). Same engine as the
  playlist reconcile, richer conflict surface (which *field* wins, not just membership). Play
  counts already flow one-way via `navi push` stats; the general version makes that base-aware.
  See [docs/design/playlist-reconcile.md](docs/design/playlist-reconcile.md) for the parked note.

### Complementary editing tools (independent of the above)

- 💡 **m3u round-trip.** Export resolved playlists as `.m3u`, edit in any external tool,
  re-import. Pro: zero bespoke UI. Con: needs stable file-path ↔ Track resolution on
  re-import, and on-disk paths must match the organized library.
- 💡 **More playlist tools in the CLI.** `merge` + `dedup-members` are designed (see *Now*);
  rename, split, reorder remain unscoped ideas. Reorder fights reconcile decision #2 (ordering
  is not a semantic), so it needs its own justification. Keeps everything in the canonical model.

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

Full design — 12 settled decisions and the three-layer build order — lives in
[docs/design/fs-reconcile.md](docs/design/fs-reconcile.md). Headlines:

- Self-repairing organize via a per-file location state machine (move from `absolute_path`,
  report missing rather than fabricate); ships first as a standalone layer.
- A `content_hash` (full-file md5) column on `TrackFile` for watch idempotency.
- `ingest_one()` per-file pipeline + `watch` (poll first, `watchdog` later) + a `reconcile`
  command (re-enrich → unify → organize). This underpins the Telegram bot's upload feature.

---

## Ingest sources

- 🧭 **Sunset the Last.fm and Spotify scrobble importers; ListenBrainz is the one scrobble
  entrance.** ListenBrainz imports listens *from* Last.fm, Spotify and friends, so anything
  those importers could read is reachable through a ListenBrainz export — including a history
  that predates the ListenBrainz account, which is backfilled there and then exported. Two
  parsers for the same plays is not redundancy we get anything for.

  **The measurement that settled it (2026-09-09, live library).** Sampling 300 dated rows of
  the real Last.fm CSV against the imported ListenBrainz history: **300/300 already present
  within ±90 s, but only 4/300 to the second.** The CSV is minute-resolution and
  `get_fresh_scrobbles` dedupes on the exact timestamp (see CLAUDE.md), so importing it would
  have added ~200k duplicate plays that nothing downstream catches. ListenBrainz is also a
  strict superset in range and volume: 2011-04-17 → 2026-06-06 / 221,601 plays, against the
  CSV's 201,456 dated rows ending 2025-12-28. **And that CSV was never an official export** —
  it came from a third-party site that crawled Last.fm, so `LastFmImporter.detect`'s
  "headerless artist,album,track,date" signature encodes one unofficial scraper's shape, not
  a format Last.fm publishes.

  **Removal is clean because both are scrobble-only** (`provides = DataKind.SCROBBLES`, no
  tracks or playlists): delete `cloud/lastfm/`, `LastFmImporter`, `SpotifyImporter` and their
  imports in `ingest/sources.py`, and the `lastfm` value from `import --as`. It also retires
  two loose auto-detect signatures — a headerless 4-column CSV and a bare JSON record list —
  that can only ever mis-claim someone's file.

  **`cloud/spotify/` is not deleted wholesale** — see the library importer below. Only its
  scrobble half goes (`scrobbles.py`: `SpotifyScrobbleParser`, `get_spotify_streaming_history`,
  `get_spotify_scrobbles`, `SpotifyRecord`). If the library importer lands, it inherits the
  `spotify` name and the `--as spotify` value, and their meaning changes with it: **the
  streaming-history export stops being an accepted input** and `detect` keys on the library
  files instead. Retire the scrobble half in the same change that adds the library one, or
  `--as spotify` silently means the wrong thing in between.

  **What stays, and why:** `_ScrobbleImporter` (still two subclasses) and `apple_ms`, which is
  not a scrobble-only source — it carries `TRACKS | PLAYLISTS | SCROBBLES`, and Apple Music
  play history has no reliable route into ListenBrainz. `AppleScrobbleParser` stays with it.

- 💡 **Spotify *library* importer (saved tracks + own playlists) from the account-data export.**
  Spotify is where the listening actually happens now, and Airdrome cannot see that library at
  all: `SpotifyImporter` is scrobble-only. Scope agreed 2026-09-09: **saved tracks
  (`YourLibrary.json`) and self-authored playlists (`Playlist*.json`)** → `SourceTrack` and
  `SourcePlaylist`/`SourcePlaylistTrack`, i.e. `provides = TRACKS | PLAYLISTS`. Saved *albums*
  are out — there is no Album entity and the export gives an album name without its track list,
  so they cannot expand. Followed playlists are out — they change under you and edits cannot go
  back.

  **Blocked on reading a real export** (requested 2026-09-09, Spotify takes days). The field
  mapping *is* the design, so the `alias_map` is not worth guessing; what we already have on
  disk is `Streaming_History_Audio_*.json` — the *extended history*, a different file with a
  different shape (`master_metadata_track_name` / `_album_album_name` / `_album_artist_name`,
  `spotify_track_uri`, `ts`, `ms_played`; no track duration).

  Three findings from scoping it, so they need not be re-derived:
  - **Identity is not a problem.** `unify` keys a `SourceTrack` onto a canonical `Track` by
    title/artist/album/album_artist — the four fields the export carries — and `_upsert_track`
    backfills NULLs from whichever source has them. Spotify rows join existing tracks and
    inherit `year`/`duration_ms`/`track_number` from the Apple or file side; ones Airdrome has
    never seen become file-less canonical tracks, which is exactly the acquire-later list the
    Telegram bot's upload feature wants.
  - **`expects_local_file` already returns `False`** for any non-`APPLE_XML` provider, so
    Spotify tracks are not reported as missing files. No change needed there.
  - **File binding still runs for them anyway** ("attempted for every source track regardless"),
    so every imported Spotify track adds ~18 more `ILIKE` scans to the pass measured above —
    and `possible_locations` rebuilds *Apple* paths from Spotify metadata, which can mis-bind a
    Spotify row to a file that merely path-matches. Either land the batching fix first, or gate
    the filename pass on providers that can actually name a file.

  One behaviour to decide when it is built, not now: with the habitual `land --merge-playlists`,
  a Spotify playlist sharing a name with an Apple one merges into it. Probably wanted, but it is
  a choice, not a default to inherit silently.
