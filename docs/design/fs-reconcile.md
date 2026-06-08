# Filesystem ⇄ Airdrome reconcile

**Status:** 🧭 designed, not built. Settled 2026-05-31 (branch `refactor`).
Summary + build status live in [ROADMAP.md](../../ROADMAP.md); this is the long form.

A self-repairing `organize`, a watch folder, and a tag `reconcile` pipeline for keeping
on-disk files and the canonical graph in sync.

## Core mental model — three hops; organize is only the last

```
file tags --enrich--> TrackFile metadata --unify--> Track identity --organize--> disk location
```

- **enrich** (`models.py` `TrackFile.enrich`): reads tags from `absolute_path` into TrackFile fields.
- **unify**: maps TrackFile metadata → Track via `get_or_create(title, artist, album, album_artist)`
  (the Track unique constraint).
- **organize** (`library/organize.py`): places the file from the **Track's** computed path
  (`generate_relative_path`), NOT from the file's tags.

Consequence: **tag changes do nothing physical until they reach the Track.**

## Settled decisions

1. **Source roots are NOT stored.** `import <path>` stays stateless about roots; provenance is
   per-`TrackFile.source_path`. Re-scan = re-run `import` with the path. Adds-only re-scan already
   works (`scan_file` = `get_or_create` on `source_path`). Future *delete* detection can scope off
   the provided path vs `source_path` prefixes (match on a path boundary, e.g. `/music/rock/` not
   bare `/music/rock`) — still no roots table. Only store roots if a no-arg `reconcile` that
   re-sweeps everything is ever wanted.
2. **Source-of-truth: BOTH copy and move, configurable** — keep the copy/move flag; reconciliation
   must work in either mode.
3. **Re-scan scope: ADDS-ONLY for now.** Defer delete/move detection.
4. **Idempotent / self-repairing organize via a per-file location state machine.** Per file:
   `desired = compute(Track metadata + role)` where role = main vs copy (`is_main`); `actual` = disk.
   Rule: desired exists → no-op; file at recorded `library_path` → relocate to desired; file at
   `source_path` → bring in; nowhere → **REPORT missing** (don't fabricate). **Key fix:** move from
   `absolute_path` (current location), not always `source_path` — this fixes move-mode `--reset`
   (today it crashes: the file already moved out of `source_path`) and enables self-heal. No new
   schema needed. Self-heal (re-copy from source when the library copy was deleted) only works in
   **copy** mode.
5. **Copies location:** optional `copies_dir: Path | None` setting (default `None` → today's
   `library_dir/Copies`). `is_main` selects which root `library_path` resolves against in
   `absolute_path`. Keeps a single relative-path concept.
6. **Quality-upgrade flow is already free:** a higher-bitrate drop with identical tags →
   `get_or_create` finds the existing Track → attaches as a 2nd TrackFile → organize's `select_main`
   (sorts by bitrate then ext) promotes it to `Library/`, demotes the old to `Copies/`. Falls out of
   (unique constraint) + (idempotent organize). No fuzzy match involved.
7. **Watch matching: NO fuzzy/trigram.** Strict identity = full-metadata equality (already the Track
   unique key). Trigram dedup (`auto_deduplicate`, which resets ALL canon_ids) stays a separate
   batch/manual step for near-misses.
8. **`content_hash` column on TrackFile** (computed in enrich) for watch idempotency: skip exact byte
   re-drops so re-dropping the same file doesn't file a 2nd identical copy. Use **full-file md5**, NOT
   an audio-stream hash. Consequence: "same audio, corrected tags" is NOT recognized as same audio
   (tags are in the bytes → md5 differs); it flows as a normal new file routed by its new tags.
   Audio-stream hash deferred.
9. **`watch <folder>`: poll-on-a-timer first** — re-sweep every N seconds, move mode so the folder
   drains (like iTunes' "Automatically Add"), calling `ingest_one` per file. Real-time `watchdog` is
   a later swap behind the same function.
10. **Tag reconcile exposed as ONE `reconcile` command** running re-enrich → unify → organize
    end-to-end. Internally still three decoupled passes.
11. **Re-enrich must be its own pass** (iterate existing TrackFiles, re-read from `absolute_path`),
    NOT through the `source_path`-keyed `scan_file` — after a move, an organized file's `source_path`
    is stale, so scanning the library via `get_or_create(source_path)` would create spurious
    duplicate TrackFiles.
12. **unify re-bind: DEFERRED.** For now re-enrich only updates TrackFile fields; the Track binding
    stays put; unify still only promotes orphans. Accepted limitation: an in-place tag edit that
    **changes identity** won't relocate the file until re-bind is built (organize keys off the
    unchanged Track).

## Build plan — three layers, each its own branch + tests

- **Layer 1 — idempotent/self-repairing organize** (the state machine, decision #4). Stands alone;
  wanted regardless. Tests: re-run is no-op; metadata change relocates; deleted library file re-heals
  from source (copy mode); both-gone reports; move-mode reset doesn't crash.
- **Layer 2 — `content_hash` column** on TrackFile (additive; decision #8).
- **Layer 3 — `ingest_one()` per-file pipeline + `watch` (poll) + `reconcile` command**
  (decisions #9–11).

Build Layer 1 first (reviewable alone), then 2, then 3. This pipeline underpins the Telegram bot's
upload feature — the bot is one front-end to `ingest_one()`.
