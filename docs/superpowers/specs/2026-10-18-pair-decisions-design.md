# Manual dedup decisions as pairs — design

Status: approved in chat by the owner, 2026-10-18. Branch `mcp-server` (the MCP server v1 is not merged yet;
this lands on the same branch, before the live rollout).

## Why

Manual dedup decisions are stored as `DedupGroup` / `DedupGroupMember`: a group is identified by the
multiset of its members' `duplicate_hash`, each member carries the `canon_hash` it was assigned, and an
all-NULL group means "not duplicates". Groups do not compose:

- The TUI saves a page as a new group keyed by its exact member multiset. When a page's membership changes,
  the old group stays beside the new one. `stored_canons` merges overlapping groups last-writer-wins in row
  order, so on one read order `dedup` silently drops a "same track" decision (repro: groups `w→v` and
  `x→w`; order-dependent outcome). Live DB, 2026-10-18: 22 clusters of overlapping groups — 13 merge
  cleanly, 5 name two canons for one group, 4 contradict an older "not duplicates" with a newer "same".
- One MCP point edit sits behind five guards in `library/edits.py` (`_affected` closure,
  `_refuse_dead_members`, `_refuse_unapplied`, collateral veto, cross-recording-id refusal); four exist only
  because of the encoding, and `_refuse_unapplied` has a one-hop-root false refusal that is a symptom of it.
- `duplicate_hash` includes mutable fields (track/disc number, year, 5 s duration bucket), so
  `ladder.backfill` refuses to fill those fields on any track named in a stored group
  (`stored_member_hashes`) — the data model bent to keep the decision key stable.

Recreating the DB now would restore the overlap faithfully, so this lands before the rollout.

## Owner's decisions (verbatim intent)

1. Replace the group encoding with **pair decisions** now, before the DB is recreated and before the Spotify
   importer ("путь 2").
2. **The newest decision wins** ("новое побеждает"). All 4 live contradictions resolve to "same", including
   Drowning Pool's "Step Up (The Punisher Version)" → Linkin Park's "Step Up", kept on purpose ("не помню
   чтобы когда-то слушал первый, а второй слушал много раз").
3. **A manual "same" beats a recording-id disagreement** ("ручное решение сильнее, пусть объединяет").
   His "same" can mean "fold this one into the one I listen to", not only "same recording". Automatic
   linking keeps the one-recording-id-per-group rule; only manual decisions cross it.
4. **Remove the TUI** ("TUI можно вообще выпиливать… основная работа будет идти через mcp"). The work goes
   through MCP. The duplicate-review MCP tools stay where the agreed order puts them (after the Spotify
   importer and the playlist tools); until then the manual-edit tools are `mark_same_track` /
   `mark_different_tracks`.
5. Everything else in the chat playback was confirmed ("В остальном все так"): the storage shape, the
   resolution rule, the natural-key identity, dead decisions kept and counted, the one-time migration from
   the live DB, the new mirror format with the old one still readable by `dedup-import`.

## Goals

- `dedup` output depends only on the decisions and the library, never on row order.
- One manual edit = one stored row (plus at most one canon row). No closure, no re-encoding of neighbours.
- Decisions survive a DB rebuild (mirror file) and survive tag backfill (key does not include mutable fields).
- Every existing decision carries over, with the contradictions resolved newest-wins.

## Non-goals

- The duplicate-review queue and its tools (confirm group, choose canon, reset, resolve dead member) —
  later, per the agreed order. This change must leave room for them, not build them.
- Changing automatic grouping (recording identity, fingerprints, flag-sets, `RECOMMENDED_SETS`).
- A history/audit UI for superseded decisions.

## Model

### Track identity in a decision

A decision names tracks by their **natural key** — `(title, artist, album, album_artist)`, the raw columns
the `Track` unique constraint is on — not by `duplicate_hash` and not by `Track.id` (ids do not survive a
DB rebuild; the hash includes mutable fields).

- Make the `Track` unique constraint `NULLS NOT DISTINCT` (PostgreSQL 15+; check the compose image), so a
  key names at most one track. Live DB, 2026-10-18: zero collisions on the 4-tuple with NULLs folded.
  Check that `get_or_create` and the ingest paths behave with it (a NULL-artist lookup must find the
  existing row, not raise on insert).
- Representation in the table is the builder's call (four columns per side, or one canonical text key such
  as compact JSON of the 4-tuple). It must round-trip exactly through the mirror and compare by equality.
- `ladder.backfill`'s `stored_member_hashes` guard and its `unify.py` use go away: backfill no longer
  touches a decision's key.

### Tables (replace `dedupgroup` / `dedupgroupmember`)

- **Pair decision**: `track_a` key, `track_b` key, `kind` (`same` | `different`), `decided_at` (tz-aware
  UTC), `origin` (`mcp` | `import` | `migrated`, informational). Stored with the pair ordered (a < b) and
  **unique per unordered pair**: a new decision on the same pair replaces the row (upsert, new
  `decided_at`).
- **Canon choice**: one track key, `decided_at`, `origin`; unique per track. Means "this track is the main
  version of whatever group it ends up in". Written by the migration now; its MCP writer comes with the
  duplicate-review tools.

### Resolution (`dedup` consumes decisions)

A pure function over (decisions, library) → manual must-link components, manual cannot-links, canon picks.
Deterministic; never depends on row order.

1. Map keys to live tracks. A decision with an end that names no live track is **dead**: ignored, kept in
   the table, counted (see `status`).
2. Walk live pair decisions **newest first** (ties broken by a fixed key order) with a union-find plus a set
   of separations between components:
   - `same(a, b)`: union a's and b's components, unless an already-accepted (hence newer) `different`
     separates them — then this decision is **superseded**.
   - `different(a, b)`: record a separation between the two components, unless they are already one
     component (a newer `same` chain joined them) — then superseded.
   When two components merge, their separations merge too, so a separation stays transitive.
3. Output: must-link components (from accepted `same`), cannot-link pairs between separated components (from
   accepted `different`), and for each must-link component the canon = the **newest** canon choice among
   its members, if any.

How `auto_deduplicate` uses it (replacing `stored_canons` / `apply_manual_overrides` /
`stored_cannot_links` / `_anchor_on_stored_canon`):

- Automatic groups are built as today (recording identity, then flag-sets), with the manual cannot-links as
  the hard transitive veto in `merge_overlapping_groups` (as `stored_cannot_links` is today).
- Manual must-link components are then unioned into the result **without** the recording-id check
  (decision 3). A must-link component that an automatic group would join to something it is separated from
  keeps the separation (manual wins over automatic in both directions).
- Canon per final group: a manual canon choice inside the group wins; otherwise `canon_order` as today.
  Links that a manual decision created carry `BindMethod.MANUAL`, confidence 1.0; others keep their
  automatic method.
- `flatten_canon_chains` and `recompute_main_files` run as today; `canon_id` stays terminal.

### Main file in a manually folded group (owner: "главным всегда файл главной версии")

A manual `same` can fold one recording into another. In such a group the main file must come from the
canon's own recording, even when a folded-in file has a higher bitrate. Definition (my call): the canon's
**core** is the canon plus the members automatic grouping alone would have joined to it (the group as it
would be without manual must-links). `select_main` / `recompute_main_files`, the `organize` upgrade swap
and import `classify`'s "upgrade" comparison all choose among the core's files. Only if the core has no
file at all does the choice fall back to the whole group (otherwise the group would play nothing). Groups
without a manual fold are unaffected (core = group). The core is derived at `dedup` time; store on `Track`
whatever the readers need (e.g. a flag on the members outside the core) rather than recomputing it in each
reader.

### MCP edits (`library/edits.py`)

- `mark_same(a, b)` / `mark_different(a, b)` upsert one pair decision (`origin=mcp`, `decided_at=now`),
  then bring `canon_id` in line for the tracks involved so the result equals what the next full `dedup`
  would produce for them. The builder chooses how (re-resolve the affected components, or a targeted
  update with a test that compares against a full `dedup`).
- Removed: `_affected`, `_refuse_dead_members`, `_refuse_unapplied`, the collateral veto, and the
  cross-recording-id refusal in `mark_same` (decision 3). Kept: id sanity (`_check_pair`, `_get`), the
  refusal of `a == b`.
- The resolver is the single source of truth: an edit's outcome is whatever the resolution gives, even
  where it differs from today's `mark_different` "detach b" (my call). It differs only when manual `same`
  chains are involved: root `a`, twins `b`, `c` via `same(a,c)` older than `same(c,b)`; a new
  `different(a,b)` then supersedes `same(a,c)` and `a` leaves, not `b`. The tool returns the resulting
  groups of both tracks, so the caller sees the outcome. Tool names and argument shapes stay.
- `track_card`'s stored-groups section becomes the pair decisions touching the card's members (kind,
  decided_at, accepted / superseded / dead) plus any canon choice.

### Mirror (`duplicates.json`)

- Same mechanics as today: whole-table snapshot inside the writing transaction, written after commit,
  empty writes an empty document, restore only into empty tables, unparsable raises.
- New shape, versioned: `{"version": 2, "pairs": [{"a": [...key], "b": [...key], "kind", "decided_at",
  "origin"}], "canons": [{"track": [...key], "decided_at", "origin"}]}`, deterministic order.
- `restore_if_empty` and `dedup-import` read version 2. One upsert rule everywhere (my call): a row with
  an older `decided_at` never replaces a newer one on the same pair / track, so a version-2 import keeps
  the file's timestamps and newest wins row by row. `dedup-import` also reads the old (version-less) group
  shape for foreign/archived files, converting each group as the migration does, with `decided_at` =
  import time in file order (importing it is the decision); the **live** groups are migrated from the DB
  (below), not from the file.

### Migration (one time, from the live DB)

- A script under `scripts/` reads the live `dedupgroup` tables (old code path, before the schema change)
  and writes a version-2 mirror. Group order = `DedupGroup.id` order (the only place creation order
  survives; the old mirror is sorted by label). Synthetic `decided_at`: all before the migration time,
  strictly increasing with group id.
- Per group: each member with a canon → `same(member, canon)`; every pair of members whose resolved canons
  differ → `different` (so an all-NULL group yields all its pairs as `different`). A canon choice only
  where a person picked it (my call — an unchosen pin would outrank `canon_order` forever): not for `mcp:`
  groups (their root was `canon_order`'s pick), and for TUI groups only where the stored canon differs
  from what `canon_order` picks for that group at migration time. Members are converted from hash to natural key through the live tracks;
  a member hash with no live track is dropped and counted.
- The script prints, for the overlapping clusters, what the resolution makes of them, so the result can be
  checked against the 22 clusters listed in chat (13 clean, 5 canon-choice, 4 contradictions → "same").
- Then: back up the old `duplicates.json`, recreate the DB, `import` → `land`, restore from the new mirror,
  `dedup`, check `status`.

### TUI removal

- Delete `normalize/dedup/manual.py`, `normalize/dedup/tui.py`, `dedup --review/-r` and `--match`, their
  exports, docs and tests (`test_dedup_manual.py`). `save_confirmed_groups` / `load_confirmed_groups` go
  with them.
- `dedup` still prints the automatic groups: move the table rendering it borrows
  (`DeduplicatorUI.compose_table`) into a plain console helper.
- If a dependency was there only for the TUI, drop it.

### Other consumers

- `terminal/status.py`: counts of same / different / canon decisions, superseded, dead (replacing
  `confirmed_groups`).
- `scripts/identity_acceptance.py`: the partial/complete group metric becomes live / dead decisions.
- Docs: `CLAUDE.md` (dedup command, *Dedup group mirror*, data flow step 4, key models, identity ladder
  bullet about stored "not duplicates"), README, `docs/design/identity-ladder.md`, the MCP spec where it
  describes stored groups, model docstrings. Record the design rationale in `docs/design/` (why pairs,
  rejected: invariant patch on groups).
- `ROADMAP.md`: move the 🔨 item to done (into CLAUDE.md as current state); the duplicate-review item no
  longer needs the refactor.

## Constraints

- Python 3.14, SQLAlchemy 2, PostgreSQL + alembic with one squashed migration (regenerate; the DB is
  recreated). Project conventions in `CLAUDE.md` (docstrings, Ruff incl. `ANN`, flush-not-commit in domain
  code, tz-aware UTC).
- Manual decisions live in the DB only (the mirror is their durable copy); never in code or config.
- Ask the owner before recreating the live DB. Back up `duplicates.json` first.
- Tests run against the compose Postgres; every behaviour above gets a test, in particular: both read
  orders of the `w→v` / `x→w` repro give the same result; newest-wins across a transitive chain; a manual
  `same` across two recording ids links; a dead decision is ignored and counted; an MCP edit's result
  equals a subsequent full `dedup`; mirror round-trip; old-format import.

## Left to the builder

Table/column representation of the key; how edits re-resolve; the migration script's shape and output;
whether superseded rows are flagged in the table or only computed; module layout (a new
`normalize/dedup/decisions.py` is the natural home for the model helpers and the resolver).
