# Identity Ladder + Fingerprints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind files and aliases to tracks through one ordered ladder of matchers (MusicBrainz
recording id → chromaprint fingerprint → tags → Apple path), record how every binding was made,
group same-recording tracks as dedup twins, and make `import <any dir>` report and import only
new recordings and upgrades.

**Architecture:** Evidence is read once per file (`enrich`: tags, ids, md5, fingerprint) and
stored on `TrackFile`. `land` binds Apple source tracks to files through an in-memory path-tail
index, then resolves every still-unbound file through the ladder. Recording identity never
re-keys or merges `Track` rows — it feeds `dedup`'s canon rebuild as one more grouping input,
under the stored manual choices.

**Tech Stack:** Python 3.14, SQLAlchemy 2, PostgreSQL (pg_trgm), mutagen, `fpcalc` 1.6 (external
binary, already on g15), typer, pytest. No new Python dependency: AcoustID HTTP goes through
`urllib.request`.

**Spec:** [docs/design/identity-ladder.md](../../design/identity-ladder.md) — read it first; this
plan argues from it.

## Global Constraints

- Work only in this workspace (`/home/me/orca/workspaces/airdrome/identity-ladder`, branch
  `work/identity-ladder`); commit after each task; no `Co-Authored-By` trailer.
- `Track` unique key (title, artist, album, album_artist) and `Track.duplicate_hash` do not change;
  no task merges or deletes `Track` rows.
- Business logic takes `s: Session`, uses `s.flush()`, never `s.commit()`. Commands touching disk
  take `dry_run` as a behavioural argument (CLAUDE.md *Session lifecycle*).
- Ruff `ANN` applies outside `tests/`; one-line docstring on every function and method.
- Tests: `uv run pytest` with `docker compose up -d`; type check `uvx ty check` against the
  standing baseline (no new diagnostics in touched files).
- Schema: edit models → delete `alembic/versions/2026_06_05_initial_schema.py` → regenerate with
  `uv run alembic revision --autogenerate -m "initial schema"` against an empty DB; keep the
  `CREATE EXTENSION pg_trgm` line.
- Values pinned by the spec: fingerprint auto-link duration gap **≤ 2 s**; AcoustID store
  threshold **score ≥ 0.9 and exactly one recording**; trigram alias threshold stays **0.4**.
- The AcoustID key is a setting (`ACOUSTID_CLIENT_KEY`), read from `.env`; never logged, never
  written to memory or the repo.

## Review Focus

1. **A file whose tags exact-match track X while the Apple path claims it for source track Y** —
   Y must win (today's behaviour). Test in Task 5.
2. **A file of a hand-made "not duplicates" group sharing a recording id with its sibling** — the
   veto must hold, including through a third track. Test in Task 7.
3. **One mis-bound file giving a Track a second recording id** — the Track must not chain two
   groups. Test in Task 7.
4. **MP4 ids returned as `bytes`** — an m4a and an mp3 of one recording must yield equal id
   strings. Test in Task 2.
5. **Re-running `import` on the same folders** — no re-fingerprinting of cached files, no second
   `TrackFile` for an already imported file, same report. Test in Task 9.

---

## File Structure

| File | Responsibility |
|---|---|
| `airdrome/enums.py` | + `BindMethod` StrEnum |
| `airdrome/models.py` | new columns; `ScanCache` model; `enrich` delegates to `library/tags.py` |
| `airdrome/conf.py` | + `acoustid_client_key: str \| None` |
| `airdrome/library/tags.py` (new) | read tags + ids from any supported container |
| `airdrome/library/fingerprint.py` (new) | `fpcalc` wrapper, similarity, in-memory index |
| `airdrome/library/enrich.py` (new) | re-enrich pass over existing `TrackFile`s |
| `airdrome/library/pathindex.py` (new) | in-memory Apple path-tail index |
| `airdrome/library/ladder.py` (new) | file → track resolution for unbound files |
| `airdrome/library/unify.py` | stage 1 uses `pathindex`; stage 3 calls `ladder` |
| `airdrome/library/scan.py` | widened extensions; batch classification for `import` |
| `airdrome/library/organize.py` | narrow upgrade swap |
| `airdrome/normalize/dedup/recording.py` (new) | recording groups + review candidates |
| `airdrome/normalize/dedup/auto.py`, `grouping.py` | wire recording groups, cannot-link, canon rule |
| `airdrome/cloud/listenbrainz/scrobble.py`, `airdrome/scrobbles/parser.py` | keep listen ids |
| `airdrome/scrobbles/match_aliases.py` | alias ladder |
| `airdrome/cloud/acoustid.py` (new) | AcoustID lookup client |
| `airdrome/terminal/maint.py`, `app.py` | `maint enrich`, `maint acoustid`, `import` report |

---

### Task 1: Schema, enum, setting

**Files:**
- Modify: `airdrome/enums.py`, `airdrome/models.py`, `airdrome/conf.py`, `.env.example` (if present, else README config table)
- Regenerate: `alembic/versions/*_initial_schema.py`
- Test: `tests/test_identity_schema.py`

**Interfaces — Produces:**
- `class BindMethod(StrEnum)`: `MBID="mbid"`, `ACOUSTID="acoustid"`, `FINGERPRINT="fingerprint"`,
  `TAGS_EXACT="tags_exact"`, `TAGS_FUZZY="tags_fuzzy"`, `APPLE_PATH="apple_path"`,
  `DEDUP_FLAGS="dedup_flags"`, `MANUAL="manual"`.
- `TrackFile`: `recording_mbid: str|None` (indexed), `mbid_source: BindMethod|None` (`MBID` = tag,
  `ACOUSTID` = lookup), `acoustid_id: str|None`, `content_hash: str|None` (indexed, md5 hex),
  `fingerprint: list[int]|None` (`ARRAY(Integer)`), `fp_duration: float|None`,
  `bind_method: BindMethod|None`, `bind_confidence: float|None`.
- `TrackAlias`: `match_method`, `match_confidence`.
- `TrackAliasScrobble`: `submitted_mbid: str|None`, `mapped_mbid: str|None`.
- `Track`: `canon_method`, `canon_confidence` (meaningful only when `canon_id` is set).
- `class ScanCache(Base)`, table `scancache`: `path` (unique), `size: int`, `mtime: float`,
  `content_hash`, `fingerprint`, `fp_duration`, `recording_mbid`.
- `Settings.acoustid_client_key: str | None = None`.

- [ ] **Step 1: Failing test** `test_new_columns_round_trip`: create a `TrackFile` with every new
  field set (fingerprint `[1, -2, 3]`, `bind_method=BindMethod.APPLE_PATH`), flush, expire,
  reload; assert each value equal. `test_scancache_path_unique`: two rows with one path →
  `IntegrityError`.
- [ ] **Step 2:** `uv run pytest tests/test_identity_schema.py -v` → FAIL (unknown attribute).
- [ ] **Step 3:** Add enum, columns (enum columns as `sa.Enum(BindMethod, native_enum=False)`,
  like `Source`), `ScanCache`, setting; regenerate the migration per Global Constraints.
- [ ] **Step 4:** Tests PASS; full `uv run pytest` PASS.
- [ ] **Step 5:** Commit `identity: schema for bindings, ids, fingerprints, scan cache`.

### Task 2: Tag + id reading for every container

**Files:**
- Create: `airdrome/library/tags.py`
- Modify: `airdrome/models.py` (`TrackFile.enrich`), `airdrome/library/scan.py`, `airdrome/ingest/sources.py` (`_AUDIO_EXTENSIONS`)
- Test: `tests/test_tags.py`

**Interfaces — Produces:**
- `AUDIO_EXTENSIONS: frozenset[str]` in `tags.py` = `{".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma"}`;
  `MusicScanner.EXTENSIONS` and `_AUDIO_EXTENSIONS` import it; suffix compare is lower-cased.
- `@dataclass(frozen=True) class FileTags`: `title, artist, album, album_artist, date,
  recording_mbid, acoustid_id: str|None`, `duration: float|None`, `bitrate: int`.
- `def extract_tags(tags: Mapping[str, object]) -> dict[str, str | None]` — pure, key mapping only.
- `def read_tags(path: Path) -> FileTags` — opens with mutagen, calls `extract_tags`.
- `def file_md5(path: Path) -> str` — full-file md5, streamed in 1 MiB chunks.
- `TrackFile.enrich()` keeps its name; sets the tag fields, `recording_mbid` (+ `mbid_source=MBID`
  when present), `acoustid_id`, `content_hash`. It does **not** fingerprint (Task 4 does).

Key map (spec): recording id — ID3 `UFID:http://musicbrainz.org` (`.data`), MP4
`----:com.apple.iTunes:MusicBrainz Track Id`, Vorbis/FLAC/Opus `musicbrainz_trackid`, ASF
`MusicBrainz/Track Id`; AcoustID id — ID3 `TXXX:Acoustid Id`, MP4 `----:com.apple.iTunes:Acoustid Id`,
Vorbis `acoustid_id`, ASF `Acoustid/Id`. Title/artist/album/album_artist/date for Vorbis
(`title`, `artist`, `album`, `albumartist`, `date`) and ASF (`Title`, `Author`, `WM/AlbumTitle`,
`WM/AlbumArtist`, `WM/Year`) beside today's ID3/MP4 keys. Every `bytes` value is decoded
`utf-8`/`replace`; list values join with `"; "` as today.

- [ ] **Step 1: Failing tests** (dict-like tag mappings built from real mutagen frame types, no
  audio files):
  - `test_mp3_and_m4a_ids_equal`: `{"UFID:http://musicbrainz.org": UFID(owner=..., data=b"abc-1")}`
    and `{"----:com.apple.iTunes:MusicBrainz Track Id": [MP4FreeForm(b"abc-1")]}` both give
    `recording_mbid == "abc-1"` (never `"b'abc-1'"`).
  - `test_vorbis_keys`: `{"title": ["T"], "albumartist": ["AA"], "musicbrainz_trackid": ["m"]}` →
    title `T`, album_artist `AA`, recording_mbid `m`.
  - `test_missing_ids_none`.
  - `test_file_md5` on a tmp file with known bytes.
  - `test_scanner_accepts_upper_and_new_suffixes`: `a.OGG`, `b.opus`, `c.wav` in `AUDIO_EXTENSIONS` check.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement; `enrich` becomes a thin caller of `read_tags` + `file_md5`.
- [ ] **Step 4:** PASS, plus existing `tests/test_ingest_detect.py`.
- [ ] **Step 5:** Commit `identity: read MusicBrainz/AcoustID ids and md5 in enrich; widen containers`.

### Task 3: Fingerprint module

**Files:**
- Create: `airdrome/library/fingerprint.py`
- Test: `tests/test_fingerprint.py` (+ a `wav` fixture factory in `tests/conftest.py`)

**Interfaces — Produces:**
- `@dataclass(frozen=True) class Fingerprint`: `raw: list[int]`, `duration: float`.
- `def compute(path: Path) -> Fingerprint` — runs `fpcalc -raw -json -length 120 <path>`;
  raises `FingerprintError` (new, `RuntimeError` subclass) on non-zero exit or missing binary.
- `def similarity(a: list[int], b: list[int], max_offset: int = 80) -> float` — best over offsets
  in `[-max_offset, max_offset]` of `1 - popcount(x ^ y) / (32 * n_overlap)`; overlap < 20 items → 0.0.
- `FP_MATCH: float = 0.85` — "same recording" threshold, calibrated in Task 11.
- `class FingerprintIndex`: `add(key: int, fp: list[int]) -> None`;
  `candidates(fp: list[int], min_shared: int = 10) -> list[int]` (keys sharing ≥ `min_shared`
  identical items among `v >> 4` values — inverted index, no pairwise scan);
  `matches(fp: list[int], threshold: float = FP_MATCH) -> list[tuple[int, float]]` (candidates
  scored with `similarity`, best first).

- [ ] **Step 1: Failing tests.** Fixture `make_wav(path, seed, seconds=30, gain=1.0, rate=44100)`
  writes deterministic band-limited noise with stdlib `wave` + `random.Random(seed)`.
  - `test_same_audio_scores_high`: seed 1 at gain 1.0 vs seed 1 at gain 0.6, 22050 Hz →
    `similarity ≥ FP_MATCH`.
  - `test_different_audio_scores_low`: seeds 1 vs 2 → `< 0.6`.
  - `test_index_finds_match_without_pairwise`: add 50 random fps + the seed-1 fp; `matches` of
    the gain-0.6 copy returns the seed-1 key first.
  - `test_compute_missing_binary` (monkeypatch `PATH=""`) → `FingerprintError`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement (`int.bit_count()` for popcount). **Step 4:** PASS.
- [ ] **Step 5:** Commit `identity: chromaprint wrapper, similarity and inverted index`.

### Task 4: `maint enrich` — re-enrich existing files

**Files:**
- Create: `airdrome/library/enrich.py`
- Modify: `airdrome/terminal/maint.py`
- Test: `tests/test_reenrich.py`

**Interfaces:**
- Consumes: `read_tags`, `file_md5` (Task 2), `compute` (Task 3).
- Produces: `def reenrich(s: Session, *, force: bool = False, workers: int = 4) -> EnrichStats` —
  iterates every `TrackFile`, reads `absolute_path` (never `source_path` lookups — fs-reconcile
  #11); skips a file whose `content_hash` is unchanged and `fingerprint` is set unless `force`;
  fingerprints in a `ThreadPoolExecutor(workers)`; a missing/unreadable file is counted, not fatal.
  Never changes `track_id`. `EnrichStats`: `seen, updated, missing, fp_failed, with_mbid, with_fp: int`.
- CLI `airdrome maint enrich [--force] [--workers N] [-n]` prints the stats as coverage
  ("recording id: 3601/6986 files").

- [ ] **Step 1: Failing tests** (wav fixtures): `test_fills_hash_and_fp`; `test_skips_unchanged`
  (second run `updated == 0`, `compute` not called — monkeypatch counter);
  `test_keeps_binding` (`track_id` unchanged); `test_missing_file_counted`.
- [ ] **Steps 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `identity: maint enrich re-reads tags, ids, hash, fingerprint`.

### Task 5: In-memory Apple path binding (replaces the ILIKE storm)

**Files:**
- Create: `airdrome/library/pathindex.py`
- Modify: `airdrome/library/unify.py` (`_bind_track_files`, `_unify_source_tracks`)
- Test: `tests/test_pathindex.py`, extend `tests/test_apple_ingest.py`

**Interfaces — Produces:**
- `class PathTailIndex`: `@classmethod build(cls, s: Session) -> PathTailIndex` loads
  `(id, source_path)` of **unbound** files once; `lookup(rel_path: str) -> list[int]` returns ids
  of files whose lower-cased POSIX `source_path` ends with `"/" + rel_path.lower()` (or equals it);
  `claim(file_id: int) -> None` removes a bound file from future results.
  Implementation: dict keyed by the last path component (lower) → list of (id, lowered full
  path); `lookup` filters that bucket by `endswith`. `_`/`%` are plain characters.
- `_bind_track_files(source_track, s, index: PathTailIndex) -> list[TrackFile]` — same contract as
  today (dedup by id, only unbound), sets `bind_method=APPLE_PATH`, `bind_confidence=0.9`.

- [ ] **Step 1: Failing tests:** `test_case_insensitive`; `test_underscore_literal`
  (`AC_DC/x.mp3` must not match `ACxDC/x.mp3`); `test_tail_hits_two_roots` (both ids);
  `test_claimed_file_not_returned`; and the Review Focus #1 test
  `test_path_beats_matching_tags`: file tagged exactly like Track X, path matching source track Y
  → after `do_unify` the file is on Y's track with `bind_method == APPLE_PATH`.
- [ ] **Steps 2–4:** FAIL → implement → PASS (incl. all existing unify tests).
- [ ] **Step 5:** Commit `land: bind Apple files through an in-memory path index`.

### Task 6: File ladder for unbound files

**Files:**
- Create: `airdrome/library/ladder.py`
- Modify: `airdrome/library/unify.py` (stage 3 `_unify_orphan_files` → ladder; drop the
  `title IS NOT NULL` filter — junk-tag files now resolve by recording)
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `FingerprintIndex`, `FP_MATCH` (Task 3); `recording_of_track` (defined here, reused in Task 7).
- Produces:
  - `def recording_of_track(t: Track) -> str | None` — the `recording_mbid` all of the track's
    files agree on; `None` if none or they disagree.
  - `@dataclass class LadderContext` built once per run: `by_mbid: dict[str, list[Track]]`,
    `fp_index: FingerprintIndex` keyed by `TrackFile.id`, `file_track: dict[int, Track]`.
    `@classmethod build(cls, s: Session) -> LadderContext`.
  - `def resolve_file(s: Session, tf: TrackFile, ctx: LadderContext) -> Track` — binds and returns.
    Order: (a) `tf.recording_mbid` in `ctx.by_mbid` → group, method `mbid`/`acoustid` per
    `mbid_source`, confidence 1.0/0.9; (b) fingerprint match ≥ `FP_MATCH` **and** `|Δduration| ≤ 2`
    → group, method `fingerprint`, confidence = similarity; (c) none → tags
    (`_upsert_track` exact, method `tags_exact`, confidence 1.0; created → same method).
    Inside a matched group (spec *The ladder — file → track*, rules 1–3): member with equal
    `album_norm` → bind there; else usable tags (title, artist, album all non-blank) → upsert
    from tags, then set that track's `canon_id` to the group canon with `canon_method` = the rung;
    else → bind to the group canon. Newly bound files are added to `ctx`.
  - A fingerprint match with `|Δduration| > 2` is **not** used here; Task 7 surfaces it.
  - `do_unify` stage 3 first fingerprints unbound files that lack one (Task 4's worker, exposed as
    `fingerprint_files(s: Session, files: list[TrackFile], workers: int = 4) -> int`), so `land`
    works without a prior `maint enrich`.

- [ ] **Step 1: Failing tests:** `test_mbid_binds_to_same_album_member`;
  `test_mbid_other_album_creates_twin` (compilation copy → new Track, twin of the album track);
  `test_junk_tags_bind_to_canon` (title `"Track 01"`, no album, fingerprint of a library file);
  `test_fp_duration_gap_not_linked` (3 s gap → falls to tags);
  `test_no_evidence_tags_exact`; `test_recording_of_track_disagreeing_files_is_none`.
- [ ] **Steps 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `land: resolve unbound files through the identity ladder`.

### Task 7: Recording identity in `dedup`

**Files:**
- Create: `airdrome/normalize/dedup/recording.py`
- Modify: `airdrome/normalize/dedup/auto.py`, `grouping.py` (`canon_order`), `manual.py` (candidate pages)
- Test: `tests/test_dedup_recording.py`

**Interfaces:**
- Consumes: `recording_of_track` (Task 6), `FingerprintIndex` (Task 3).
- Produces:
  - `def compute_recording_groups(s: Session) -> tuple[list[list[Track]], list[list[Track]]]` —
    `(auto, review)`. Auto: tracks sharing `recording_of_track`; tracks whose main/any files
    fingerprint-match at ≥ `FP_MATCH` with `|Δduration| ≤ 2`. Review: fingerprint matches with a
    larger gap. Conflicted tracks (`recording_of_track is None` but files carry ids) are
    excluded and returned through `log`.
  - `def stored_cannot_links(s: Session) -> set[frozenset[int]]` — track-id pairs from stored
    groups whose members have different resolved canons (incl. all-`NULL` "not duplicates").
  - `merge_overlapping_groups(..., cannot_link: set[frozenset[int]] = frozenset())` — a union
    that would put a cannot-link pair in one component is skipped.
  - `auto_deduplicate` prepends recording groups (label `rec[mbid|fp]#j`) to the flag-set groups,
    passes `stored_cannot_links`, sets `canon_method` (`mbid`/`fingerprint`/`dedup_flags`) on
    twins; `apply_manual_overrides` sets `canon_method=MANUAL`. `dedup --review` shows `review`
    groups as unconfirmed pages.
  - `canon_order` gains a leading key: **not a compilation, unless the owner has ≥ 2 tracks of
    that compilation** — a track is a compilation member when `compilation is True` or
    `album_artist_norm in {"various artists", "various"}`; count per `album_norm` over all tracks.
    Implemented as a SQL `CASE` over a per-album count subquery so the query-time order and the
    merge re-query agree (CLAUDE.md *Dedup tuning notes*).

- [ ] **Step 1: Failing tests:**
  - `test_same_mbid_two_albums_one_group` (album track is canon).
  - `test_feed_the_addiction_stays_apart` (different ids, fingerprints < threshold).
  - `test_compilation_canon_when_two_or_more` (two NFS tracks from one compilation → compilation canon).
  - Review Focus #2 `test_not_duplicates_veto_transitive`: A, B stored as "not duplicates";
    C shares an id with A and with B → A and B end in different groups.
  - Review Focus #3 `test_conflicted_track_does_not_chain`.
  - `test_recording_groups_survive_rerun` (run `auto_deduplicate` twice, same canons).
  - `test_mirror_hashes_unchanged` (`duplicate_hash` of every track identical before/after).
- [ ] **Steps 2–4:** FAIL → implement → PASS, incl. existing `tests/test_dedup_*.py`.
- [ ] **Step 5:** Commit `dedup: recording identity as a grouping input with manual veto`.

### Task 8: Alias ladder with listen ids

**Files:**
- Modify: `airdrome/cloud/listenbrainz/scrobble.py`, `airdrome/scrobbles/parser.py`,
  `airdrome/scrobbles/match_aliases.py`, `airdrome/match.py`
- Test: extend `tests/test_match_aliases.py`; new `tests/test_lb_ids.py`

**Interfaces:**
- `ListenBrainzScrobble` gains `submitted_mbid` (`additional_info.recording_mbid`) and
  `mapped_mbid` (`mbid_mapping.recording_mbid`).
- `ScrobbleParser._iterate_scrobbles` yields `ScrobbleRecord(alias: TrackAlias, at: datetime,
  submitted_mbid: str|None = None, mapped_mbid: str|None = None)` (new dataclass in `parser.py`);
  other parsers yield it with ids `None`. `get_fresh_scrobbles` copies the ids onto
  `TrackAliasScrobble`.
- `def alias_ids(s: Session, alias: TrackAlias) -> tuple[str|None, str|None]` — unanimous
  submitted id, unanimous mapped id across its scrobbles (disagreement → `None`).
- `do_match_aliases` order: submitted id ∈ library (`LadderContext.by_mbid`) → group canon,
  `match_method=MBID`, 1.0; exact normalized title+artist(+album when present) → `TAGS_EXACT`, 1.0;
  `find_best_track` → `TAGS_FUZZY`, confidence = score. The mapped id may only (i) add `+0.1`
  (capped 1.0) when it equals the fuzzy pick's recording, (ii) break a tie between equal-score
  candidates — `find_best_track` gets `prefer_mbid: str | None = None`. It never binds alone.

- [ ] **Step 1: Failing tests:** `test_lb_parser_keeps_both_ids` (JSONL line with both);
  `test_submitted_id_binds`; `test_mapped_id_alone_does_not_bind` (no text match → unbound);
  `test_mapped_id_breaks_tie`; `test_disagreeing_listens_no_id`.
- [ ] **Steps 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `aliases: bind by player-submitted ids; mapped ids only confirm`.

### Task 9: `import <dirs>` — report, import new + upgrades, swap

**Files:**
- Modify: `airdrome/library/scan.py`, `airdrome/ingest/sources.py` (`MusicFolderImporter`),
  `airdrome/library/organize.py`, `airdrome/terminal/app.py`
- Test: `tests/test_import_classify.py`, extend `tests/test_organize.py`

**Interfaces:**
- `class Verdict(StrEnum)`: `NEW`, `HAVE`, `UPGRADE`.
- `@dataclass class Candidate`: `paths: list[Path]` (all copies seen), `best: Path`,
  `tags: FileTags`, `fp: Fingerprint`, `content_hash: str`, `verdict: Verdict`, `group_canon: Track|None`.
- `def probe(s: Session, path: Path) -> ScanCache` — cache hit when (size, mtime) match, else read
  + fingerprint + upsert the cache row.
- `def classify(s: Session, roots: list[Path]) -> list[Candidate]` — walk roots
  (`AUDIO_EXTENSIONS`), skip files already a `TrackFile.source_path` or whose `content_hash` a
  `TrackFile` has; collapse repeats by `content_hash`, then by fingerprint (≥ `FP_MATCH`, ≤ 2 s);
  best copy per `TrackGroup.select_main_file` rule; match against the library like Task 6 (id →
  fingerprint → tags exact); verdict `UPGRADE` when the best copy's bitrate (kbps) exceeds the
  group main file's, `HAVE` otherwise, `NEW` when unmatched.
- `MusicFolderImporter.import_tracks` → `classify`, prints one line per candidate
  (`new | have | upgrade  artist — title  [n copies]`) and totals; for `NEW`/`UPGRADE` creates a
  `TrackFile` for `best` and resolves it with `resolve_file` (Task 6). `HAVE` writes nothing but the cache.
  `-n` already rolls back; the report is identical either way.
- `FileOrganizer.swap_upgrade(main: TrackFile, new: TrackFile, t: Track) -> None` — the old main's
  file moves `Library/` → `Copies/` (next free suffix), the new one goes into the freed `Library/`
  path; `is_main` flips; honours `dry_run`. `transfer_track` calls it when the group's current
  `is_main` file already has a `library_path` and `select_main` picks a different, unplaced file.
- `MusicScanner.scan_all` (used by the PicardedMusic-style first import) stays and gains nothing
  but the widened extensions; `classify` is the folder importer's path when the library already
  has files, `scan_all` when it has none.

- [ ] **Step 1: Failing tests** (wav fixtures + a library seeded with one track/file):
  `test_repeats_collapse` (same bytes in two dirs → one candidate, 2 paths);
  `test_reencoded_copy_is_have` (lower "bitrate" copy, same fp);
  `test_better_copy_is_upgrade_and_swaps` (after import + organize: new file in `Library/`, old in `Copies/`);
  `test_unknown_is_new`;
  Review Focus #5 `test_rerun_uses_cache_and_adds_nothing` (`compute` called 0 times on run 2, no new `TrackFile`);
  `test_dry_run_swap_touches_nothing`.
- [ ] **Steps 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `import: classify loose folders, import new recordings and upgrades`.

### Task 10: `maint acoustid` — lookup command

**Files:**
- Create: `airdrome/cloud/acoustid.py`
- Modify: `airdrome/terminal/maint.py`
- Test: `tests/test_acoustid.py`

**Interfaces:**
- `def lookup(client_key: str, fingerprint: str, duration: int) -> list[tuple[float, list[str]]]` —
  POST `https://api.acoustid.org/v2/lookup` with `meta=recordingids`; returns
  `(score, recording_ids)` per result. The compressed fingerprint comes from `fpcalc -json`
  (without `-raw`), computed on demand here — it is not stored.
- `def identify(s: Session, client_key: str, *, limit: int | None = None) -> IdentifyStats` — for
  files with a stored fingerprint and no `recording_mbid`; ≤ 3 requests/s; stores
  `recording_mbid`, `acoustid_id`, `mbid_source=ACOUSTID` **only** when the top result has
  score ≥ 0.9 and exactly one recording id; otherwise counts it under `ambiguous` and lists it
  with `-v`. No submissions.
- CLI `airdrome maint acoustid [--limit N] [-n]`; exits with a clear message when
  `settings.acoustid_client_key` is unset. The key never appears in output or logs.

- [ ] **Step 1: Failing tests** (monkeypatch `lookup`): `test_stores_confident_single`;
  `test_two_recordings_ambiguous`; `test_low_score_ambiguous`; `test_rate_limit_spacing`
  (fake clock); `test_missing_key_message`.
- [ ] **Steps 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `maint: AcoustID lookup fills recording ids above 0.9`.

### Task 11: Acceptance on the live library + docs

**Files:**
- Create: `scripts/identity_acceptance.py` (snapshot + compare; not shipped in the package)
- Modify: `CLAUDE.md`, `README.md`, `ROADMAP.md`, `docs/design/identity-ladder.md` (status → built),
  `docs/design/fs-reconcile.md` (#12 note)

- [ ] **Step 1:** From the **current** live DB, snapshot `file source_path → sorted source_paths
  of all files in its dedup group` to a JSON in the scratchpad (read-only).
- [ ] **Step 2:** Point `DB_DSN` at a fresh database `airdrome_ladder` (never the live one);
  re-import every source from `~/Music/Airdrome/{apple,listenbrainz,spotify,lastfm}` and
  `~/Music/PicardedMusic`, `dedup-import ~/Music/Airdrome/.airdrome/duplicates.json`
  if needed, run `maint enrich`, `land` (time step 1), `dedup`.
- [ ] **Step 3: Calibrate `FP_MATCH`:** among file pairs with equal tag recording ids vs pairs with
  different ids that the index proposes, report the similarity distributions; set `FP_MATCH`
  between them and record the numbers in the design doc. Re-run Step 2's `dedup` if it moved.
- [ ] **Step 4: Compare:** all 6,986 files bound; per file, the new group ⊇ the old group; every
  group that grew is listed with the rung that grew it; method distribution printed per binding
  kind; `land` step 1 in seconds. Any file whose group *lost* members is a failure — stop and
  report.
- [ ] **Step 5:** `import -n` over a scratch folder with a re-encoded copy of a library file and
  one unknown wav → `have`/`new`.
- [ ] **Step 6:** Docs: CLAUDE.md (*Data flow*, *Normalization & matching*, CLI surface for
  `maint enrich`/`maint acoustid`/`import` report, config row `ACOUSTID_CLIENT_KEY`), README
  (commands, `fpcalc` requirement, AcoustID key), ROADMAP (remove the item, keep `wanted` next),
  fs-reconcile #12 note, design status 🧭 → ✅ with the calibrated threshold.
- [ ] **Step 7:** `uv run pytest`, `ruff check . && ruff format --check .`, `uvx ty check` (no new
  diagnostics in touched files); commit `identity ladder: acceptance numbers and docs`; push.
