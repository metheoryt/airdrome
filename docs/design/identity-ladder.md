# Identity ladder + fingerprints

**Status:** ✅ built 2026-10-17 (branch `work/identity-ladder`); `FP_MATCH` calibrated to **0.89** — see
*Built: calibration and acceptance* at the end. Settled in chat with the owner 2026-10-16.
First item of ROADMAP *Identity & acquisition*; `wanted` and Spotify-as-a-live-remote follow and
are out of scope here.

## Why

Track identity is built from strings and paths today, and every subsystem patches that its own
way: `land` rebuilds Apple's on-disk path and `ILIKE`s it against every file (~490k scans, ~40 min
for step 1), aliases match by weighted `pg_trgm`, `Track` is keyed on the exact
(title, artist, album, album_artist) tuple, and dedup buckets by flag sets. Nothing records *how*
a binding was made, so nothing can be audited or upgraded.

The fix is one ordered **ladder** of matchers, strongest evidence first, with every binding
recording which rung made it. Two pieces of evidence the library already has but throws away make
the ladder worth it:

- **MusicBrainz recording ids in tags** — measured 2026-10-16: 3,601 of the 6,986 files in
  `~/Music/PicardedMusic` carry one (321 also carry an AcoustID id). `TrackFile.enrich` reads
  neither today.
- **Recording ids on ListenBrainz listens** — `ListenBrainzScrobble` drops them. Most are
  ListenBrainz's own text matches over Last.fm imports and count only as a weak hint; 1,109
  listens carry a player-submitted id (see *alias → track*).

And one the library can compute: a **chromaprint fingerprint** per file (`fpcalc` is installed on
g15), which identifies untagged files — folders of loose mp3s (copied off old CDs/DVDs and the like),
scattered and repeated across folders in varying combinations. `import` takes folders.

## Goals

1. `TrackFile` carries `recording_mbid`, `acoustid_id`, `fingerprint`, `content_hash`, read or
   computed once and stored.
2. File→track and alias→track bindings go through one ladder; twin→canon links made from
   recording identity join the existing dedup groups. Every binding records `method` + `confidence`.
3. `import <any directory>` works with no iTunes layout assumed, and reports per recording:
   **new / already have / upgrade**. Without `-n` it imports new recordings and upgrades only.
4. A separate command looks fingerprints up in AcoustID and stores the returned recording id in
   the DB.
5. `land` step 1 drops from ~40 min to seconds by running the path rebuild in memory instead of
   as an `ILIKE` storm.

## Non-goals (this step)

- **Grouping file-less "ghost" tracks by recording id** — belongs to `wanted` (agreed in chat).
  This step only *stores* the ids on aliases and binds aliases by the player-submitted one.
- **Writing MBIDs into files.** DB only for now; writing tags is a later, separate opt-in.
- **AcoustID submissions.** Lookups only.
- PostgreSQL → SQLite (deferred until after the ladder), Spotify remote, `wanted`, Soulseek.
- A new on-disk layout for `organize` (it still mirrors Apple's). Moving already-organized files
  when a binding changes stays deferred (fs-reconcile #12).

## Anti-goals (would be a failure even if it works)

- **Losing a binding on the live library.** Every one of the 6,986 currently bound files must stay
  in the same dedup group after the change, except merges the new rungs add on purpose, which are
  reported.
- **Breaking the 881 hand-made dedup groups or the mirror.** They are keyed on
  `Track.duplicate_hash` (title, artist, album_artist, album, track_n, disc_n, year, duration
  bucket). Anything that merges or re-keys `Track` rows orphans them.
- **Requiring Picard-tagged input.** Half the library is untagged, the loose-mp3 folders entirely.
- **A silent wrong merge that is hard to find.** Every automatic merge must be listable by method.

## Decided

### What a `Track` is — unchanged

A `Track` stays "a track on a specific release": the unique key (title, artist, album,
album_artist) does not change and rows are never merged. **Recording identity groups tracks; it
does not re-key them.** The same recording on the original album and on a compilation/OST is two
`Track` rows that become twins in one dedup group (one play count, one main file). This is what
keeps `duplicate_hash`, the 881 groups and the mirror valid.

Different recordings stay apart by construction: the album and *NFS Most Wanted* versions of
*Feed the Addiction* have different recording ids and different audio, and no rung joins them.

### Canon choice inside a group

Owner's rule: **prefer the original album; a compilation/OST wins when the owner has ≥2 tracks
from that compilation** (NFS series, Shaun White Snowboarding, Daredevil). A manual choice in
`dedup --review` overrides both, as today. This becomes a leading key in `canon_order`, ahead of
the existing date/year keys.

### The ladder — file → track

Two passes, in today's order (the `unify.py` module docstring explains why the order matters):

1. **Apple source binding first.** Each Apple source track claims its own files through the
   path rebuild (see below) before any file is resolved by its own evidence. A file claimed here
   is bound to its source track even when its tags exact-match another Track — the source
   track wins, as today.
2. **The file ladder for every file still unbound:** **recording id → fingerprint → tags**.
   Creating a new Track from tags is the *last* step of the whole ladder, never earlier.

A recording-level rung (id, fingerprint) answers *which group*; the release is then chosen
inside it:

1. The matched group has a member on the file's own album (normalized album equal) → bind there.
2. Else, the file's tags are usable (title + artist + album present) → `get_or_create` the Track
   from the tags as today, and make it a twin of the group (the twin link records the rung).
3. Else (junk tags such as "Track 01") → bind to the group's canon.

No recording-level match → **tags**: exact normalized-key match to an existing Track, else a new
Track from tags (today's `_unify_orphan_files` behaviour).

**Apple path rebuild, in memory.** `possible_locations` candidates are matched against an
in-memory index of `TrackFile.source_path` tails — one dict pass, case-insensitive, `_`/`%`
literal, a tail may hit several files (dedup by id), only unbound files bind. This replaces
`_bind_track_files`' `ILIKE` loop and is what makes `land` step 1 take seconds. **On by default**
— the owner reversed the 2026-10-16 "opt-in" decision on the evidence below; it was opt-in only
because it was slow, and in memory it is not.

Measured why the path rung still matters (2026-10-16, live DB):

- 6,739 of 6,986 files (96.5%) have tags whose normalized title/artist/album equal their track's;
  the other **247 reach their track only through the path**.
- The Apple XML export carries **no `Location`** (3 of 9,796 tracks), so there is no exact-path
  rung to replace the rebuild. Apple Media Services rows have none by format.
- XML `size` equals the file's current size for only 86 of those 247 (Picard rewrote the tags),
  so a size rung does not replace it either.
- Fingerprints cannot rescue them — an Apple source track has no audio to compare.

Without the path rung those 247 files would each mint a duplicate file-only Track beside a
file-less Apple ghost — the split the `unify.py` docstring warns about.

### The ladder — alias → track

**submitted recording id → exact normalized tags → trigram fuzzy** (today's `find_best_track`,
threshold 0.4). An alias resolves by id when some file in the library carries that id; the result
is the group's canon.

**Scrobble ids are mostly guesses — do not lean on them** (owner, 2026-10-17). A ListenBrainz
listen carries two kinds of id, and they are not equal:

- `additional_info.recording_mbid` — sent by the player that knew the file's id (Navidrome,
  BrainzPlayer). Real evidence. Only **1,109** listens have it.
- `mbid_mapping.recording_mbid` — ListenBrainz's own server-side match from the text, present on
  208,435 listens. **201,443** of the 231,232 listens came in through the Last.fm importer, and
  Last.fm gives no ids at all, so for them this is a text guess by someone else's matcher.

So only the submitted id is a ladder rung (method `mbid`). The mapped id is stored on the alias
as a separate, weak field and may only **confirm** a fuzzy match (raise its confidence, break a
tie between equally scored candidates); it never binds an alias on its own and never groups
tracks. `wanted`'s ghost grouping inherits the same rule.

### Automatic vs reviewed merges by fingerprint

A fingerprint match **links silently when the durations differ by ≤ 2 s**. A match with a larger
duration gap (radio edit vs album version, remaster, long shared intro) is **not linked**; it is
offered as a candidate group in `dedup --review`. Every automatic link is listable by method and
undoable through the existing review path.

Recording ids by provenance (*designer's call*):

- **From a tag** (Picard put it there) → links silently.
- **From the AcoustID lookup** → its own method value `acoustid`. Stored and linked silently only
  when the top result has score ≥ 0.9 and maps to exactly one recording; otherwise nothing is
  stored and the file is listed for review.
- **A Track's recording id** is the id its files agree on. Files of one Track carrying different
  ids → the Track has none for grouping, and the conflict is reported. Same rule for an alias
  whose listens disagree. This stops one mis-bound file from chaining two groups together.

**A manual choice beats every automatic link** (*designer's call*). A stored "not duplicates"
review is a hard veto: the recording-identity union may not join those tracks, directly or
through a third track (cannot-link constraint in the union-find). A newcomer that recording
identity adds to one of the hand-made groups joins under that group's stored canon.

### Recording-identity groups survive `dedup`

`dedup` rebuilds every `canon_id` from flag sets plus stored manual overrides. Recording identity
therefore becomes a **grouping input of that rebuild** (always on, ahead of the flag sets), not a
one-off write at `land`, or the next `dedup` would erase it. `land` may set the same links early;
`dedup` reproduces them.

### Fingerprints: both modes

- **Local.** `fpcalc` runs once per file; the result is stored on `TrackFile`. Local comparison
  answers "do I already have this recording" offline.
- **AcoustID lookup** is its own command, run on demand. It fills `recording_mbid` (and
  `acoustid_id`) for files that lack one, in the DB only. Note: AcoustID's lookup API takes an
  **application (client) key**, not the account's user key (the user key is only for
  submissions). The owner registers an application under his account; the key goes in `.env` as
  a new setting and never into chat, memory or the repo.

### `import <dirs>` for loose files

- Any directory; `MusicScanner.EXTENSIONS` and the folder importer's detection widen to
  ogg/opus/wav/aiff/wma, and tag reading covers those containers.
- Repeats are collapsed first: identical bytes by `content_hash`, identical audio by fingerprint.
  The report lists each recording once, with the best copy found.
- Classification per recording: **new** (no match), **have** (the library's best file is as good
  or better), **upgrade** (higher bitrate than the group's main file — the same rule as
  `TrackGroup.select_main_file`).
- Writing follows the repo convention: default writes, `-n` reports only. New recordings and
  upgrades are imported; an upgrade becomes the group's main file at `organize` and the old file
  moves to `Copies/`. "Have" copies are **not** imported.
- **That swap is in scope** (*designer's call*): today `organize` skips any file that already has a
  `library_path` and `transfer` refuses an occupied destination, so this step adds one narrow
  relocation — old main `Library/` → `Copies/`, upgrade into `Library/` — honouring `--dry-run`.
  General relocation on re-bind stays deferred (fs-reconcile #12).
- Files that are not imported must not be re-fingerprinted on every run: keep a cache keyed by
  (path, size, mtime) holding hash + fingerprint. Losing it (DB recreate) only costs recompute.

### Enrich and re-enrich

`enrich` additionally reads the recording id and AcoustID id, computes `content_hash` (full-file
md5, fs-reconcile #8) and the fingerprint. Recording-id tag keys (Picard's mapping):
ID3 `UFID:http://musicbrainz.org`; MP4 `----:com.apple.iTunes:MusicBrainz Track Id`; Vorbis/FLAC
`musicbrainz_trackid`. AcoustID id: ID3 `TXXX:Acoustid Id`, MP4
`----:com.apple.iTunes:Acoustid Id`, Vorbis `acoustid_id`. **mutagen returns MP4 freeform and
UFID values as bytes — decode explicitly**; `str(bytes)` yields `"b'…'"` and silently breaks
equality between an m4a and an mp3 of the same recording.

Existing files get the new fields through a **re-enrich pass over existing `TrackFile`s** reading
`absolute_path` (fs-reconcile #11), not through `scan_file` (keyed on a possibly stale
`source_path`). It is the natural first build step: it changes no binding and produces the
coverage numbers.

### Method + confidence: columns, not a binding table

Each object has exactly one binding and is always read together with it, so the record sits next
to it: `TrackFile` (file→track), `TrackAlias` (alias→track), `Track` (twin→canon). `method` is one
enum shared by all three (e.g. `mbid`, `acoustid`, `fingerprint`, `tags_exact`, `tags_fuzzy`,
`apple_path`, `dedup_flags`, `manual`); `confidence` is a float in [0, 1].

**Upgrading a binding, this step:** when a stronger rung *agrees* with the current binding, its
method and confidence replace the weaker ones. When it *disagrees*, the binding is not changed —
the disagreement is reported (moving a file between tracks would need the deferred re-bind and
relocation of fs-reconcile #12).

### Schema change

Additive columns plus the cache table; per repo rules the DB is recreated (squash the migration,
re-import, re-land). The dedup mirror re-seeds the 881 groups.

## Acceptance

On a copy of the live library after re-import + `land` + `dedup`:

- all 6,986 files bound; each file's dedup group equals today's, except merges added by the
  id/fingerprint rungs, which are listed;
- the method distribution is printed per binding kind;
- `land` step 1 finishes in seconds, not ~40 min;
- `import -n` over a folder of untagged mp3s that duplicate library tracks reports them as
  *have*/*upgrade*, not *new*.

## Relation to other docs

Partly supersedes fs-reconcile decision #12 (re-bind deferred): the ladder is the re-bind
mechanism, but in this step it only re-binds unbound files and upgrades agreeing bindings. Update
`fs-reconcile.md` when this lands. Folds in ROADMAP *Batch the file-binding pass in `land`*
(the in-memory path rung is that fix).

## Left to the builder

- How local fingerprint comparison scales (pairwise over ~7k files plus every new batch must not
  be a naive O(n²) Python loop) and the exact similarity measure/threshold for "same recording".
- How "compilation" is detected for the canon rule (`Track.compilation`, album artist "Various
  Artists", …).
- When an alias's listens carry conflicting recording ids: treat the alias as having none.
- Command and flag names, progress output, the cache's home.
- Whether fingerprinting during `import` runs in parallel and how many workers.

## Built: calibration and acceptance (2026-10-17)

Measured on a fresh database rebuilt from the live library's sources (Apple XML + Media Services,
both ListenBrainz exports, `~/Music/PicardedMusic`), then `maint enrich` → `land` → `dedup`, compared
file by file against a snapshot of the live database (`scripts/identity_acceptance.py`). Last.fm and
Spotify scrobbles were left out because the live database has none (ROADMAP *Ingest sources*: the
Last.fm CSV would add ~200k duplicate plays); they carry no tracks or files.

### Calibration of `FP_MATCH`

Pairs of files with a fingerprint and a *tag* recording id (3,583 files, 3,436 ids):

| | pairs | min | p5 | median | p95 | max |
|---|---|---|---|---|---|---|
| same id (all pairs) | 152 | 0.516 | 0.907 | 0.970 | 1.000 | 1.000 |
| different id, proposed by the index | 124 | 0.498 | 0.568 | 0.666 | 0.976 | 0.999 |

Same-id pairs below 0.89 are 4 (0.516, 0.635, 0.682, 0.685 — a remix tagged with the original's id,
and live/studio pairs Picard gave one id); **no same-id pair scores between 0.685 and 0.890**.
Different-id pairs between 0.85 and 0.89 are other versions (8-track demo vs EP, video vs radio
mix, intro vs outro). Auditing the twin links the first `dedup` made at 0.85 found **8 false links,
all between 0.850 and 0.884** — vocal vs instrumental (Korn *Path of Totality*, Fort Minor *Rising
Tied*) and *Downtown Swinga '96* vs *Part Two* — and no true link in that band. So the threshold
sits in the gap **(0.884, 0.890]: `FP_MATCH = 0.89`**. The gap is narrow (0.006).

**Above ~0.9 the distributions overlap and no threshold separates them:** 29 different-id pairs
score ≥ 0.90 (17 of them within the 2 s duration gate) — clean vs explicit, demos re-released on
anniversary editions, MusicBrainz duplicate recordings, and at least one instrumental (*Frgt/10*,
0.904). Chromaprint compares the first 120 s of harmony; versions sharing it look identical. What
keeps these apart is the recording-id veto (two different ids never join) and the duration gate,
not the threshold. Raising `FP_MATCH` would mainly cost true remaster matches (Meteora vs its 20th
anniversary edition score 0.89–0.91).

### Acceptance numbers

- **All 6,986 files bound**; 0 files whose dedup group lost a member. 30 groups grew (each
  listable by method): 21 by `mbid` (tag ids — mostly Linkin Park *Live in Texas* vs the Meteora
  20th-anniversary live tracks, plus *Slam* vs its *Bionyx Remix*, which Picard tagged with one id),
  8 by `fingerprint` — compilation/OST copies (*Daredevil*, two *Need for Speed*), a box-set
  re-release, a single vs its album, a title typo (*BEAUTIFUL LIE*/*LIFE*), and two doubtful ones:
  *Frgt/10* instrumental vs the Reanimation mix (0.904, see above) and *High Voltage (WTC Mix)*
  (linked at 0.946 through another member of its group) — 1 by both (*Faint*, live in Texas).
- Stored hand-made groups: **881 of 881 complete** in the rebuilt DB (0 partial, 0 absent).
- Method distribution — file → track: `apple_path` 6,949, `tags_exact` 21, `fingerprint` 15,
  `mbid` 1. Alias → track: `tags_exact` 14,290, `tags_fuzzy` 11,931, `mbid` 543, unmatched 52,287
  (26,764 matched vs 26,745 live). Twin → canon: `dedup_flags` 2,133, `manual` 880, `mbid` 53,
  `fingerprint` 31.
- **`land` step 1: 119 s** (source tracks 96 s, playlists 16 s, ladder 8 s) instead of ~40 min.
  Step 3 (aliases) still takes 42:51 — a separate ROADMAP item.
- `maint enrich --workers 8`: 5 min; recording id on 3,601 files, fingerprint on 6,955 (31 files
  make `fpcalc` exit non-zero on a decode error; the one checked still printed a fingerprint
  first, which `compute` then discarded — those files are bound by path anyway. Since the final
  review a usable exit-3 fingerprint is kept, and `fpcalc` runs under a timeout).
- `import -n` over a scratch folder: a library mp3 with its tags stripped → **have**; two
  separately encoded copies (319 vs 320 kbps) from an old CD-rip folder, tags stripped → **have**;
  a synthetic wav → **new**.

### Where the build differs from the text above

- The Apple path rung matches a tail on a path-component boundary (the old `ILIKE '%…%'` also hit
  mid-component); acceptance found no file lost by it.
- Upgrading an *agreeing* binding's method to a stronger rung is not built: only unbound files go
  through the ladder.
- A file with no recording-level match and no title stays unbound (counted), rather than minting a
  Track from nothing.
- The newcomer anchor (a newcomer joins a hand-made group under its stored canon) applies only to
  groups a recording-identity group contributed to; flag-set newcomers behave as before.
- Alias ids: listens without an id abstain (the alias's id-bearing listens must agree); the mapped
  id may break a tie among exact-tag candidates as well as fuzzy ones.
- `import` fingerprints library files that lack a fingerprint before classifying, and a re-run
  reports what it imported as *have*.
- `ACOUSTID_CLIENT_KEY` is a `SecretStr`, and no command prints locals in a crash dump.
- Goal 5 said "seconds" for `land` step 1; it takes 119 s (about 20× faster than before, but not
  seconds — the source-track stage alone is 96 s).
- An ambiguous AcoustID result is not "listed for review": it is counted, and named only in a
  verbose (`-v`) detail line.
- `import` still imports a new file that has no title and matches nothing, unbound, and says so on
  its own summary line (tag it, then `maint enrich` + `land`).
- Neither the ladder nor unify's source-track stage backfills a hashed field (track/disc number,
  year, duration) on a track a stored dedup group names: a changed `duplicate_hash` would silently
  drop the hand-made group's choice.

**Known limit.** The ladder's fingerprint rung matches a file against *every* bound file, while
`dedup`'s recording pass compares one representative file per track (its main, else its
lowest-id fingerprinted file). A twin link `land` made through a non-representative file can
therefore fail to reappear at the next `dedup`, which rebuilds every link from scratch. Expected
to be rare (it needs two files of one track with materially different audio); such a pair falls
back to the flag-sets or `dedup --review`.
