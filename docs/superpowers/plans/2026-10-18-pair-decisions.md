# Pair Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `DedupGroup` / `DedupGroupMember` encoding of manual dedup decisions with pair decisions and canon choices keyed by the track's natural key. The newest decision wins, and the result never depends on row order. The one-time migration from the live DB, a v2 mirror and the removal of the TUI come with it.

**Architecture:**
- A pure module, `airdrome/normalize/dedup/decisions.py`, holds the key, the decision dataclasses, the resolver and the v2 document.
- `legacy.py` reads the old group shape, and does so without the old ORM classes. Both the migration script and `dedup-import` use it.
- After the controller checkpoint, the new tables land next to the old ones. One cut-over task then switches every reader and writer at once: `auto_deduplicate`, MCP edits, the track card, the mirror and the import.
- The main-file core rule follows. The last code tasks delete the TUI and the old tables.

**Tech Stack:** Python 3.14, SQLAlchemy 2.0.50, PostgreSQL 18 (compose image `postgres:18`), alembic 1.18, Typer, FastMCP, and pytest against the compose Postgres.

**Spec:** `docs/superpowers/specs/2026-10-18-pair-decisions-design.md`

## Global Constraints

- Every function and method gets a one-line docstring. Line length is 110. Ruff includes `ANN`, so annotate everything. No bare `Any` outside `models.py` / `cloud/sources.py`.
- Domain code calls `s.flush()`, never `s.commit()`. Datetimes are timezone-aware UTC; a naive `decided_at` is a `ValueError`.
- Manual decisions live in the DB only, and the mirror is their durable copy. Never put them in code or config.
- Schema changes regenerate the single squashed migration. Delete `alembic/versions/2026_10_18_initial_schema.py`, then run `uv run alembic revision --autogenerate -m "initial schema"` against an empty DB, and keep the `CREATE EXTENSION IF NOT EXISTS pg_trgm` line.
- Every task ends with `uv run pytest` fully green (compose Postgres up) and `ruff check . && ruff format --check .` clean.
- Commit after each task on `mcp-server`, with no `Co-Authored-By` trailer (the branch's practice). Never force-push.
- This plan never recreates, migrates or writes the live DB. The checkpoint reads it read-only. Recreating it is the owner-gated rollout after this plan:
  1. back up `duplicates.json`;
  2. ask the owner;
  3. recreate the DB;
  4. run `import` and `land`;
  5. restore the v2 file;
  6. run `dedup`;
  7. check `status`.
- `dedup` output depends only on the decisions and the library, never on row order.
- The resolver is the single source of truth. An MCP edit's result equals the result of a following `dedup` run with the CLI defaults (`RECOMMENDED_SETS`, `CanonStrategy.ADDED`).

## Review Focus

1. **A re-import of a track with a NULL artist, album or album artist.** Under `NULLS NOT DISTINCT`, `get_or_create` must find the existing row. Before, a second row was quietly allowed; now an insert raises. Test: `test_get_or_create_finds_null_field_row` (Task 5).
2. **The live mirror is still in the old, version-less format when the rebuilt DB first runs `dedup` or the MCP server.** `restore_if_empty` must raise and leave the file untouched. It must not import nothing, and the next commit must not overwrite 885 groups with an empty v2 file. Test: `test_restore_refuses_legacy_file_and_leaves_it_untouched` (Task 6).
3. **Manual `same(A, B)` plus manual `different(B, X)`, where flag-sets would auto-join A and X.** The manual components go into the union-find first, so the auto join is vetoed, and the manual `same` is not. Test: `test_manual_same_beats_auto_join_to_separated_track` (Task 6).
4. **An MCP edit on a rebuilt DB whose decisions were restored but not yet `dedup`-ed (all `canon_id` NULL).** The guard `_refuse_unapplied` used to cover this. Now the edit must produce exactly what a full `dedup` would. Test: `test_edit_on_undeduped_db_matches_full_dedup` (Task 6).
5. **`dedup-import` run twice on the same v2 file.** An equal `decided_at` keeps the stored row, so the second run reports everything as kept and changes nothing. Test: `test_reimport_same_file_is_noop` (Task 6).

---

### Task 1: Decision key, dataclasses and the resolver (pure)

**Files:**
- Create: `airdrome/normalize/dedup/decisions.py`
- Modify: `airdrome/enums.py` (add `DecisionKind`, `DecisionOrigin`)
- Test: `tests/test_dedup_decisions.py` (no DB; build the `live` map by hand)

**Interfaces:**
- Consumes: `airdrome.models.Track` (only for `track_key`).
- Produces (all in `decisions.py` unless noted):
  - `DecisionKind(StrEnum)`: `SAME = "same"`, `DIFFERENT = "different"`.
  - `DecisionOrigin(StrEnum)`: `MCP = "mcp"`, `IMPORT = "import"`, `MIGRATED = "migrated"`. Both live in `enums.py`.
  - `Key = tuple[str, str | None, str | None, str | None]`, ordered `(title, artist, album, album_artist)` like the unique constraint.
  - `track_key(t: Track) -> Key`, built from the raw columns, not the `_norm` ones.
  - `key_text(k: Key) -> str` = `json.dumps(list(k), ensure_ascii=False, separators=(",", ":"))`. It is the only stored and compared form, and `None` stays distinct from `""`.
  - `parse_key(text: str | list) -> Key`. It takes the text form or the mirror's list form. It raises `ValueError` unless the value is 4 items: a `str` title and three `str | None`.
  - `@dataclass(frozen=True) Pair(a: str, b: str, kind: DecisionKind, decided_at: datetime, origin: DecisionOrigin)`, with `a < b` in Python string order.
  - `pair(x: str, y: str, kind: DecisionKind, decided_at: datetime, origin: DecisionOrigin) -> Pair`. It orders the ends and raises `ValueError` on `x == y` or a naive `decided_at`.
  - `@dataclass(frozen=True) Canon(track: str, decided_at: datetime, origin: DecisionOrigin)`.
  - `Outcome(StrEnum)`: `ACCEPTED`, `SUPERSEDED`, `DEAD`.
  - `@dataclass(frozen=True) Resolution` with these fields:
    - `components: list[frozenset[int]]`: track ids joined by accepted `same`, size ≥ 2, sorted by min id;
    - `cannot_link: frozenset[frozenset[int]]`: every cross pair of track ids between two separated components (singletons included);
    - `canons: dict[int, datetime]`: live track id → the `decided_at` of its canon choice;
    - `outcomes: dict[tuple[str, str], Outcome]`: one entry per `(a, b)`;
    - `dead_canons: int`.
  - `resolve(pairs: Iterable[Pair], canons: Iterable[Canon], live: Mapping[str, int]) -> Resolution`. `live` maps key text → live track id.

- [ ] **Step 1: Write the failing tests**
  - `test_key_text_round_trips_and_keeps_none_apart_from_empty`: `parse_key(key_text(("t", None, "", None))) == ("t", None, "", None)`, and `key_text(("t", None, None, None)) != key_text(("t", "", None, None))`.
  - `test_pair_orders_ends_and_rejects_self_and_naive`: `pair(y, x, …)` puts the smaller string in `a`. `pair(x, x, …)` and a naive datetime raise `ValueError`.
  - `test_repro_both_read_orders_agree`: keys `v, w, x`, with `same(w, v)` at t1 and `same(x, w)` at t2. `resolve` over `[p1, p2]` and over `[p2, p1]` returns equal `Resolution`s, both with `components == [frozenset({v, w, x})]`.
  - `test_newest_different_supersedes_older_chain`: `same(a, b)`@t1, `same(b, c)`@t2, `different(a, c)`@t3. Result:
    - `components == [frozenset({b, c})]`;
    - `frozenset({a, b})` and `frozenset({a, c})` are in `cannot_link`;
    - the outcome for `(a, b)` is `SUPERSEDED`;
    - the other two outcomes are `ACCEPTED`.
  - `test_newest_same_supersedes_older_different`: `different(a, c)`@t1, `same(a, b)`@t2, `same(b, c)`@t3. Result: one component `{a, b, c}`, an empty `cannot_link`, and `(a, c)` is `SUPERSEDED`.
  - `test_separation_merges_with_components`: `different(a, x)`@t3 is accepted first, then `same(x, y)`@t2. Then `same(a, y)`@t1 is `SUPERSEDED`, because the separation followed `x` into `{x, y}`.
  - `test_dead_decision_ignored_and_counted`: a pair with one end missing from `live`, and a canon on a missing key. The pair's outcome is `DEAD`, it is in no component, and `dead_canons == 1`.
  - `test_ties_break_by_key_order`: two contradictory pairs with equal `decided_at` resolve the same way in both input orders.
  - `test_canons_map_live_tracks`: `canons == {id_of(k): t}` for a live canon key.
- [ ] **Step 2: Run** `uv run pytest tests/test_dedup_decisions.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement.** Walk the live pairs sorted by `(-decided_at, a, b)`. Use a union-find over track ids, where each root holds a set of separated roots. On a union, merge the two separation sets and re-point every separation that named the old root. Apply the spec's rules: a `same` is superseded when its components are separated, and a `different` is superseded when its ends are already one component. Build `cannot_link` from the final separation sets.
- [ ] **Step 4: Run** the same tests, then `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(dedup): pair decisions keyed by natural key, newest-wins resolver`.

### Task 2: v2 document and old-group conversion (pure)

**Files:**
- Modify: `airdrome/normalize/dedup/decisions.py` (v2 document)
- Create: `airdrome/normalize/dedup/legacy.py` (pure half only in this task)
- Test: `tests/test_dedup_decisions.py`, `tests/test_dedup_legacy.py`

**Interfaces:**
- Consumes: Task 1's `Pair`, `Canon`, `pair`, `key_text`, `parse_key`, `DecisionKind`, `DecisionOrigin`.
- Produces:
  - In `decisions.py`:
    - `MIRROR_VERSION = 2`.
    - `dump_v2(pairs: Iterable[Pair], canons: Iterable[Canon]) -> dict` produces `{"version": 2, "pairs": [{"a": [...], "b": [...], "kind", "decided_at", "origin"}], "canons": [{"track": [...], "decided_at", "origin"}]}`. Keys are in list form, `decided_at` is `isoformat()` with its offset, pairs are sorted by `(a, b)` and canons by `track`. Empty input gives `{"version": 2, "pairs": [], "canons": []}`.
    - `load_v2(data: dict) -> tuple[list[Pair], list[Canon]]`. It raises `ValueError` on a missing or other version, a bad key, an unknown kind or origin, or a naive or unparsable `decided_at`.
    - `is_legacy(data: object) -> bool`: a `dict` without `"version"`.
  - In `legacy.py`:
    - `@dataclass(frozen=True) LegacyGroup(label: str | None, members: list[tuple[str, str | None]])`, holding `(member_hash, canon_hash)` pairs.
    - `@dataclass Converted(pairs: list[Pair], canons: list[Canon], dropped: int, ambiguous: int)`.
    - `legacy_groups(data: dict) -> list[LegacyGroup]` reads the old file shape `{label: {"members": [...], "canon_hashes": [...]}}` in file order. `{}` gives `[]`.
    - `legacy_hash(title: str, artist: str | None, album_artist: str | None, album: str | None, track_n: int | None, disc_n: int | None, year: int | None, duration: int | None) -> str` is the `duplicate_hash` formula, copied so legacy reading never needs the property.
    - `convert_group(group: LegacyGroup, keys_of_hash: Mapping[str, list[str]], pick: Callable[[list[str]], str], at: datetime, origin: DecisionOrigin) -> Converted`. `keys_of_hash` lists live key texts in track-id order. `pick` returns `canon_order`'s choice among key texts.
- Conversion rules (from the spec, plus rulings marked *ruling*):
  - A member hash resolves to its `canon_hash`, or to itself when that is NULL. A member whose own hash, or whose resolved hash, has no live key is dropped and counted in `dropped`.
  - `same(k, canon_key)` for every key `k` of a member with a canon. `canon_key = keys_of_hash[canon_hash][0]`, the first track by id, as `stored_canons` did.
  - `different` for every key pair across two live member hashes whose resolved hashes differ. An all-NULL group therefore yields all its pairs as `different`. Two keys with the same hash get no decision between them, as today.
  - `ambiguous` counts member hashes with more than one live key. *Ruling:* such a hash expands to all its keys.
  - Canon choices are made per distinct live canon hash `c`. *Ruling:* per sub-group (`c` plus the members that resolve to `c`), not per whole TUI group. The rule is:
    - none at all when `label` starts with `"mcp:"`;
    - otherwise `Canon(keys_of_hash[c][0], at, origin)` only when `pick(sub-group keys) != keys_of_hash[c][0]`.
  - Every pair and canon from one group carries `at`.

- [ ] **Step 1: Write the failing tests**
  - `test_v2_round_trip`: `load_v2(dump_v2(ps, cs))` returns `ps` sorted by `(a, b)` and `cs` sorted by `track`, including a key with `None` fields and a non-ASCII title. `dump_v2` of shuffled input is equal.
  - `test_v2_empty_document`: `dump_v2([], []) == {"version": 2, "pairs": [], "canons": []}`.
  - `test_load_v2_rejects_bad_input`, parametrized over:
    - version 3;
    - a missing version;
    - a 3-item key;
    - kind `"maybe"`;
    - a naive `decided_at`.

    Each raises `ValueError`.
  - `test_legacy_hash_matches_property`: equals `Track(...).duplicate_hash` for a transient `Track`, both with all fields set and with all optional fields `None`.
  - `test_convert_twin_group`: members `[(h1, None), (h2, h1)]` give one `same(k2, k1)` and no `different`.
  - `test_convert_all_null_group_is_all_different`: three hashes give three `different` pairs.
  - `test_convert_forest_group`: roots `r1`, `r2` and twin `t → r1` give `same(t, r1)`, plus `different` for `(r1, r2)` and `(t, r2)`.
  - `test_convert_drops_dead_members`: a member hash with no key, and a member whose canon hash has no key. Neither yields any decision, and `dropped == 2`.
  - `test_canon_choice_only_where_a_person_picked_it`, three cases:
    - a `"mcp:3:9"` group yields no canon even when `pick` differs;
    - a TUI group whose canon equals `pick` yields none;
    - a TUI group whose canon differs from `pick` yields exactly one `Canon` for that canon's key.
  - `test_legacy_groups_keep_file_order`: labels come back in file order, and `{}` gives `[]`.
- [ ] **Step 2: Run** `uv run pytest tests/test_dedup_decisions.py tests/test_dedup_legacy.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the signatures above. Neither module imports `DedupGroup`, `DedupGroupMember` or `persistence`.
- [ ] **Step 4: Run** the same tests, then `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(dedup): v2 decision document and old-group conversion`.

### Task 3: Migration from the live group tables

**Files:**
- Modify: `airdrome/normalize/dedup/legacy.py` (DB half)
- Create: `scripts/migrate_dedup_groups.py`
- Test: `tests/test_dedup_legacy.py`

**Interfaces:**
- Consumes:
  - Task 2's `LegacyGroup`, `convert_group`, `legacy_hash` and `dump_v2`;
  - Task 1's `resolve` and `key_text`;
  - `grouping.canon_order`;
  - `mirror.write_mirror(data: dict, path: Path)`, whose signature stays fixed through Task 6.
- Produces (in `legacy.py`):
  - `LEGACY = MetaData()`, the module's own, never `AirdromeBase.metadata`. It holds Core tables `dedupgroup(id, label)` and `dedupgroupmember(id, group_id, member_hash, canon_hash)`, matching today's columns.
  - `read_legacy_groups(conn: Connection) -> list[tuple[int, LegacyGroup]]`, in `dedupgroup.id` order.
  - `@dataclass LiveTracks(id_of_key: dict[str, int], keys_of_hash: dict[str, list[str]], name_of_key: dict[str, str])`, where a name is `"artist — title [album]"`.
  - `live_tracks(conn: Connection) -> LiveTracks`. It selects explicit `Track` columns, never whole entities: the live schema may differ from the ORM on unrelated columns.
  - `canon_pick(conn: Connection, live: LiveTracks) -> Callable[[list[str]], str]`, built on `select(Track.id).where(...).order_by(*canon_order())`.
  - `@dataclass Cluster(group_ids: list[int], kind: str, text: str)`. `kind` is one of `"clean"`, `"canon-choice"` or `"contradiction"`.
  - `@dataclass Migration(pairs: list[Pair], canons: list[Canon], groups: int, dropped: int, ambiguous: int, shared_keys: int, clusters: list[Cluster])`. `shared_keys` counts natural keys that more than one live track carries; the live constraint still treats NULLs as distinct. Expect 0, and the checkpoint stops if it is not.
  - `migrate(conn: Connection, now: datetime) -> Migration`.
  - `format_report(m: Migration) -> str`.
  - `write_migration(conn: Connection, out: Path, now: datetime) -> Migration`. It refuses (`ValueError`) an `out` that exists or resolves to `settings.duplicates_file`, then runs `migrate` and writes `write_mirror(dump_v2(m.pairs, m.canons), out)`.
- Rules:
  - The group at index `i` of `n` (id order) gets `at = now - timedelta(seconds=n - i)`: all before `now`, strictly increasing.
  - When two groups produce the same `(a, b)` or the same canon key, the later group's row is kept.
  - A **cluster** is a connected component of ≥ 2 groups that share a member hash. Its `kind` is decided in this order:
    1. `contradiction` if some `(a, b)` gets both `same` and `different` across its groups;
    2. else `canon-choice` if its groups name ≥ 2 distinct canon hashes for tracks that `resolve` puts in one component;
    3. else `clean`.
  - Each cluster's `text` lists:
    - every group (id, label, member names, each member's canon name or `root`);
    - what `resolve` makes of it: the components by name, the separations, and the canon (the canon choice, or "canon_order at dedup time");
    - every superseded decision.
  - The report ends with totals: groups read, `same` / `different` / canon counts, dropped, ambiguous, shared keys, and clusters per kind.
- Script: `uv run python scripts/migrate_dedup_groups.py OUT [--dsn DSN]`, a thin `main(argv: list[str] | None = None) -> int`.
  - `OUT` is required; `--dsn` defaults to `settings.db_dsn`.
  - It checks the `OUT` refusals before it connects.
  - It runs `SET TRANSACTION READ ONLY` as the first statement, calls `write_migration`, prints `format_report(m)`, and rolls back. It never commits and never calls `upgrade_to_head`.

- [ ] **Step 1: Write the failing tests.** Run them on `session.connection()` after `LEGACY.create_all(conn, checkfirst=True)`. Insert old rows with Core `insert`, and tracks with `make_track`.
  - `test_migrate_repro_is_order_independent`: groups `w→v` and `x→w` inserted with ids in both orders (two runs, explicit ids). `resolve` over each run's output gives one component `{v, w, x}`.
  - `test_migrate_newest_group_wins_contradiction`: an older all-NULL group `{a, b}` and a newer group `b → a`. The cluster's kind is `"contradiction"`, and the resolution joins `a` and `b`.
  - `test_migrate_timestamps_increase_with_group_id`: every `decided_at` is `< now`, and a later group id gets a later timestamp.
  - `test_migrate_canon_choice_rule`: an `mcp:` group, a TUI group whose canon equals `canon_order`'s pick, and a TUI group whose canon does not. Exactly one `Canon` comes out, for the third.
  - `test_migrate_counts_dropped_members`: a member hash with no live track gives `dropped == 1`.
  - `test_write_migration_writes_v2_and_refuses_existing_out`: `load_v2(json.loads(out.read_text()))` succeeds. A second call on the same `out` raises `ValueError`, and so does `out = settings.duplicates_file`.
  - `test_script_refuses_live_mirror_path`: `main([str(settings.duplicates_file)])` returns non-zero without connecting (monkeypatch `create_engine` to raise).
- [ ] **Step 2: Run** `uv run pytest tests/test_dedup_legacy.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `legacy.py`'s DB half and the thin script.
- [ ] **Step 4: Run** the same tests, then `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(dedup): one-time migration of stored groups to v2 pair decisions`.

### Task 4: Controller checkpoint: run the migration against the live DB (no code)

- [ ] **Step 1:** With the live `.env` (`DB_DSN` = the live DB), run the command below. It is read-only, and the live mirror is not touched.

  ```bash
  S=/tmp/claude-1000/-home-me-my-airdrome/fdd994a6-0ecc-4b15-a3ff-5774cef5e5fe/scratchpad
  uv run python scripts/migrate_dedup_groups.py "$S/duplicates.v2.json" | tee "$S/migration-report.txt"
  ```
- [ ] **Step 2:** Check the totals:
  - 885 groups read, `shared keys: 0`;
  - **22 clusters: 13 clean, 5 canon-choice, 4 contradiction**;
  - all 4 contradictions resolve to "same", including Drowning Pool "Step Up (The Punisher Version)" joining Linkin Park "Step Up";
  - "Linkin Park — Step Up [Under Attack (B-Sides)]" and "… [Hybrid Theory EP]" end up as one component.

  If any count differs, stop and report the difference to the owner with the report. Do not tune the resolver or the classifier to fit.
- [ ] **Step 3:** Keep the produced file and the report in the scratchpad, and tell the owner where they are. This plan does not recreate the live DB (see Global Constraints). The rollout should re-run the script just before recreation, so that MCP edits made after this checkpoint are included.

### Task 5: Schema: decision tables, NULLS NOT DISTINCT, core flag

**Files:**
- Modify: `airdrome/models.py`:
  - add `PairDecision` and `CanonChoice`;
  - on `Track`, add `canon_folded` and make the unique constraint `postgresql_nulls_not_distinct=True`;
  - keep `DedupGroup` / `DedupGroupMember` until Task 9.
- Create: `airdrome/normalize/dedup/store.py`
- Modify: `tests/factories.py` (add `add_pair`, `add_canon`, and fix the docstring that relies on NULL-distinct)
- Replace: the squashed migration (regenerate)
- Modify: every test that creates two tracks with the same `(title, artist, album, album_artist)` under NULLs. Give them distinct albums.
- Test: `tests/test_decision_store.py`

**Interfaces:**
- Consumes: Task 1's `Pair`, `Canon`, `key_text`, `track_key`, `resolve` and `Resolution`; Task 2's `dump_v2`.
- Produces:
  - `PairDecision` (table `pairdecision`): `id`, `key_a: str`, `key_b: str`, `kind: DecisionKind` (`native_enum=False`), `decided_at: AwareDatetime`, `origin: DecisionOrigin` (`native_enum=False`), and `UniqueConstraint("key_a", "key_b")`.
    - *Ruling:* no DB `CHECK (key_a < key_b)`. Postgres collation order is not Python's, so all ordering is in Python (`pair()`).
  - `CanonChoice` (table `canonchoice`): `id`, `key: str` (unique), `decided_at`, `origin`.
  - `Track.canon_folded: Mapped[bool] = mapped_column(default=False, server_default=sa.false())`. It is True for a twin outside its canon's core. Task 6 writes it and Task 7 reads it.
  - `store.py`:
    - `live_keys(s: Session) -> dict[str, int]`, from 4 columns, no entity load;
    - `load_pairs(s) -> list[Pair]` and `load_canons(s) -> list[Canon]`;
    - `Upsert(StrEnum)`: `CREATED`, `REPLACED`, `KEPT`;
    - `upsert_pair(s, p: Pair) -> Upsert` and `upsert_canon(s, c: Canon) -> Upsert`. These replace a row only when the incoming `decided_at` is **strictly** newer. *Ruling:* equal means kept;
    - `export_decisions(s) -> dict`, which is `dump_v2` of both tables;
    - `resolve_library(s) -> Resolution`.
  - `tests/factories.py`:
    - `add_pair(s, t1: Track, t2: Track, kind: DecisionKind = SAME, at: datetime | None = None, origin: DecisionOrigin = MCP) -> PairDecision`;
    - `add_canon(s, t: Track, at: datetime | None = None) -> CanonChoice`.

    A default `at` comes from a module counter that is strictly increasing per call (base + n seconds), so the order of calls is the decision order.

- [ ] **Step 1: Write the failing tests**
  - `test_track_key_is_unique_with_nulls`: two `Track(title="x")` with every other key field `None` raise `IntegrityError` on flush.
  - `test_get_or_create_finds_null_field_row`: `Track.get_or_create(s, title="x", artist=None, album=None, album_artist=None)` twice returns the same row, with `created` False the second time. Do the same through `ladder.upsert_track`.
  - `test_upsert_pair_newest_wins_never_older`: insert at t2. Re-upserting at t1 gives `KEPT` with the row unchanged. At t2 it gives `KEPT`. At t3 with the other kind it gives `REPLACED`, and the row has the new kind and t3. Either argument order of the ends hits the same row.
  - `test_upsert_canon_newest_wins`: the same three cases for `CanonChoice`.
  - `test_export_decisions_round_trips`: `load_v2(export_decisions(s))` equals `(load_pairs(s), load_canons(s))`.
  - `test_resolve_library_sees_live_tracks`: `add_pair` on two tracks, plus a raw `PairDecision` whose `key_b` names no track. One component and one `DEAD` outcome.
- [ ] **Step 2: Run** `uv run pytest tests/test_decision_store.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the models and `store.py`. Grep `Track(` in `airdrome/`, and confirm each creation path looks the row up by the 4-tuple first (`get_or_create` / `upsert_track`). `filter_by(artist=None)` renders `IS NULL`.
- [ ] **Step 4: Run** `uv run pytest`. Fix every test the new constraint breaks by giving the colliding tracks distinct albums; change no assertion. Expected: PASS.
- [ ] **Step 5: Regenerate the migration.** Check that the new file contains `postgresql_nulls_not_distinct=True` on the `track` unique constraint, plus `pairdecision`, `canonchoice` and `canon_folded`. Run `uv run pytest` again.
- [ ] **Step 6: Commit** `feat(models): pair-decision and canon-choice tables; track key NULLS NOT DISTINCT`.

### Task 6: Cut-over: dedup, edits, card, mirror and import read and write decisions

This is one task on purpose. `auto_deduplicate`, the edits and the mirror must switch together, or an edit stops surviving `dedup` (tests `test_*_survives_dedup`), or the mirror snapshots the wrong table.

**Files:**
- Modify:
  - `airdrome/normalize/dedup/auto.py`: plan/apply split; remove `_anchor_on_stored_canon` and the use of `stored_canons` / `stored_cannot_links` / `apply_manual_overrides`.
  - `airdrome/normalize/dedup/persistence.py`:
    - `stash_mirror_snapshot` stores `export_decisions(s)`;
    - delete `export_dedup_groups`, `import_dedup_groups` and `apply_manual_overrides`, together with their tests in `tests/test_dedup_persistence.py`;
    - keep `stored_canons`, `stored_cannot_links`, `save_confirmed_groups` and `load_confirmed_groups` until Tasks 8–9.
  - `airdrome/normalize/dedup/store.py`: add the import functions.
  - `airdrome/normalize/dedup/mirror.py`.
  - `airdrome/normalize/dedup/__init__.py`: exports.
  - `airdrome/library/edits.py`.
  - `airdrome/library/card.py`.
  - `airdrome/terminal/pipeline.py`: `dedup` summary line, `dedup-import`.
  - `airdrome/mcpserver/server.py`: rename `seed_dedup_groups` → `seed_decisions`; rewrite the `mark_same_track` / `mark_different_tracks` / `track_card` docstrings.
- Test (rewrite to `add_pair` / `add_canon`):
  - `tests/test_dedup_auto.py`;
  - `tests/test_library_edits.py` (drop the guard tests: dead member, not applied, collateral veto, two recording ids, absorbs intersecting group, closes over pulled-in root);
  - `tests/test_dedup_mirror.py`;
  - `tests/test_card.py`;
  - `tests/test_mcp_server.py`;
  - `tests/test_cli_defaults.py`;
  - in `tests/test_dedup_recording.py`, the group-applying `auto_deduplicate` cases (`:232`, `:337`, `:357`, `:372`). Delete the two `_anchor_on_stored_canon`-only cases (`:394`, `:414`), which become `test_canon_choice_*` below;
  - in `tests/test_dedup_integration.py`, `:19` and `:91`.
  - The tests that call `stored_canons` / `stored_cannot_links` directly (`test_ladder.py:489`, `test_import_classify.py:331`, `test_apple_ingest.py:510`, `test_dedup_recording.py:224`) stay until Task 9.

**Interfaces:**
- Consumes:
  - Task 5's `store.py`, `PairDecision`, `CanonChoice` and `Track.canon_folded`;
  - Task 1's `Resolution` and `pair`;
  - Task 2's `load_v2`, `is_legacy`, `legacy_groups` and `convert_group`;
  - Task 3's `live_tracks` and `canon_pick`.
- Produces:
  - In `auto.py`:
    - `@dataclass DedupPlan` with these fields:
      - `groups: list[list[Track]]`: final groups, canon first;
      - `links: dict[int, tuple[int, BindMethod, float | None]]`: twin id → (canon id, method, confidence);
      - `folded: frozenset[int]`;
      - `raw: list[tuple[str, list[Track]]]`;
      - `resolution: Resolution`;
      - `conflicted: list[Track]`.
    - `plan_dedup(s: Session, flag_sets: list[dict[str, bool]], strategy: CanonStrategy) -> DedupPlan`. It reads no `canon_id`, so it needs no reset.
    - `apply_plan(s: Session, plan: DedupPlan, scope: AbstractSet[int] | None = None) -> set[int]`. It writes `canon` (through the relationship), `canon_method`, `canon_confidence` and `canon_folded` for the tracks in `scope` (all when None). It returns the old and new root ids it touched.
    - `edit_scope(s: Session, plan: DedupPlan, seeds: AbstractSet[int]) -> set[int]`: the closure of `seeds`, to a fixpoint, over `plan.raw` groups and the current canon groups (`group_of`).
    - `auto_deduplicate(session, flag_sets=None, strategy=ADDED) -> AutoDedupResult`. It keeps its signature: reset, `plan_dedup`, `apply_plan`, `flatten_canon_chains`, `recompute_main_files`.
    - `AutoDedupResult(groups, auto_twins, manual_links, superseded, dead, conflicted)`. `manual_changes` becomes `manual_links` (twins linked MANUAL). `dead` counts dead pairs plus dead canons.
  - In `store.py`:
    - `@dataclass ImportCounts(created: int, replaced: int, kept: int)`;
    - `import_decisions(s, pairs: Iterable[Pair], canons: Iterable[Canon]) -> ImportCounts`, which upserts and then calls `stash_mirror_snapshot`;
    - `import_legacy(s, data: dict, now: datetime) -> tuple[ImportCounts, Converted]`. The group at file index `i` gets `now + timedelta(microseconds=i)`, with origin `IMPORT` and the same canon-choice rule as the migration. Keys come from `live_tracks(s.connection())`.
  - In `mirror.py`: `restore_if_empty(session, path) -> int`, which returns rows restored (pairs + canons). Its rules:
    - no-op when either table has a row, there is no file, or the file is `{}`;
    - raises `ValueError` naming `dedup-import` and `scripts/migrate_dedup_groups.py` on any other version-less file;
    - otherwise `import_decisions(*load_v2(data))`.

    An empty table still writes the empty v2 document, not a skip.
  - In `edits.py`: `mark_same(s, a_id, b_id) -> Track` (the merged group's canon) and `mark_different(s, a_id, b_id) -> None`. Each one:
    1. runs `_check_pair`;
    2. upserts one `Pair` (`origin=MCP`, `decided_at=datetime.now(UTC)`);
    3. runs `plan_dedup(s, RECOMMENDED_SETS, CanonStrategy.ADDED)`;
    4. runs `apply_plan` on `edit_scope(…, {a_id, b_id})`;
    5. runs `recompute_main_files` on the touched roots;
    6. runs `stash_mirror_snapshot`.

    Removed: `_affected`, `_refuse_dead_members`, `_refuse_unapplied`, `_store`, the collateral veto and the recording-id refusal.
  - In `card.py`: `"stored_groups"` is replaced by `"decisions": {"pairs": [...], "canons": [...]}`.
    - Each pair is `{"a": {"key": [...], "track_id": int | None}, "b": {...}, "kind", "decided_at", "origin", "status"}`, where `status` is the `Outcome` value. It lists every pair with an end among the members' keys, newest first.
    - Each canon is `{"key", "track_id", "decided_at", "origin", "status"}` (`accepted` | `dead`).
- `plan_dedup` rules (the spec's *Resolution* section, pinned):
  1. `res = resolve_library(s)`.
  2. Build `raw` in this order: manual components (`("manual#i", tracks)`), then recording groups, then flag-set groups. Merge with `merge_overlapping_groups(s, raw, strategy, cannot_link=res.cannot_link)`. Manual first is what makes manual win in both directions: an auto join to a separated track is vetoed, while a manual `same` is never vetoed and crosses recording ids.
  3. Merge the auto part again on its own (`raw` without manual components, same `cannot_link`). The **core** of a final group with canon `c` is the group ∩ `c`'s auto-only component (or `{c}`), and `folded` is the group minus its core.
  4. Canon: the member with the newest `res.canons` entry (*ruling:* it applies to whatever final group the track is in, even outside any must-link component). Otherwise `group[0]` by `canon_order`.
  5. Method. *Ruling:* a twin gets `MANUAL`, `1.0` when it is in `folded` or shares a manual component with its canon. Otherwise it gets its recording evidence or `DEDUP_FLAGS`, as today.

- [ ] **Step 1: Write the failing tests**
  - In `tests/test_dedup_auto.py`:
    - `test_repro_both_insert_orders_same_canons`: `w→v` / `x→w` as `same` pairs inserted in both orders (two sessions or a reset between). `{t.id: t.canon_id}` after `auto_deduplicate` is equal, with `v`, `w`, `x` in one group.
    - `test_newest_wins_across_transitive_chain`: Task 1's chain, through `auto_deduplicate`. `b` and `c` share a root, and `a` is alone.
    - `test_manual_same_links_across_recording_ids`: two tracks whose files carry different `recording_mbid`, plus `same`. One group, twin `canon_method == MANUAL`, confidence 1.0.
    - `test_manual_same_beats_auto_join_to_separated_track` (Review Focus 3).
    - `test_dead_decision_ignored_counted_and_kept`: `result.dead == 1`, and the `PairDecision` row still exists after `dedup`.
    - `test_canon_choice_wins_over_canon_order`, and `test_newest_canon_choice_wins_in_merged_group`.
  - In `tests/test_library_edits.py`:
    - `test_edit_equals_full_dedup`, parametrized over `mark_same` / `mark_different`. Start from a library already deduped with `auto_deduplicate(s, RECOMMENDED_SETS)`, holding recording groups, flag-set groups and older decisions. Let `scope = edit_scope(s, plan_dedup(…), {a, b})`.
      - After the edit (and `s.expire_all()`), snapshot `{track: (canon_id, canon_method, canon_folded)}` for the tracks in `scope`, and `{file: is_main}` for their files.
      - Run `auto_deduplicate(s, RECOMMENDED_SETS)` and snapshot again. The two snapshots are equal.
      - Second assertion: every track outside `scope` has the same state after the edit as before it.
    - `test_edit_on_undeduped_db_matches_full_dedup` (Review Focus 4). Start with every `canon_id` NULL, and compare the same in-scope snapshot. Outside the scope, tracks stay untouched (NULL), as the spec promises equality only for the tracks involved.
    - `test_different_supersedes_older_chain_member`: root `a`, twins `b`, `c` from `same(a, c)` older than `same(c, b)`. After `mark_different(a, b)`, `a` is alone and `b`, `c` share a root.
    - Keep `test_same_is_idempotent`, `test_different_detaches_canon` and `test_*_survives_dedup`, rewritten so they no longer assert the group encoding.
    - `test_same_writes_one_row`: exactly one `PairDecision` after `mark_same` on a fresh pair, and no `CanonChoice`.
  - In `tests/test_dedup_mirror.py`:
    - `test_mirror_written_after_commit` writes a v2 file;
    - `test_clearing_every_decision_writes_empty_v2`;
    - `test_restore_seeds_empty_tables`;
    - `test_restore_leaves_non_empty_tables_untouched`;
    - `test_restore_refuses_legacy_file_and_leaves_it_untouched` (Review Focus 2);
    - `test_restore_noop_on_legacy_empty_object`;
    - `test_mirror_round_trip`: restore, then commit, reproduces a byte-identical file;
    - `test_rollback_drops_the_snapshot`.
  - In `tests/test_dedup_mirror.py` (import):
    - `test_import_v2_keeps_file_timestamps_and_newest_wins`: a stored row newer than the file's is kept, and an older one is replaced.
    - `test_reimport_same_file_is_noop` (Review Focus 5).
    - `test_import_legacy_file`: a version-less file with a twin group and an all-NULL group gives the expected `same` / `different` rows. Later file entries get later `decided_at`, and origin is `IMPORT`.
  - In `tests/test_card.py`: `test_card_lists_decisions_with_status`, covering an accepted, a superseded and a dead pair touching the group, plus a canon choice, each with its `status`.
  - In `tests/test_mcp_server.py`: rewrite the seed tests to `seed_decisions` with a v2 mirror. `test_edit_after_rebuild_keeps_mirrored_decisions`: the mirror holds a pair, the tables are emptied, then an MCP edit. Both pairs end up in the DB and in the file.
- [ ] **Step 2: Run** `uv run pytest tests/test_dedup_auto.py tests/test_library_edits.py tests/test_dedup_mirror.py tests/test_card.py tests/test_mcp_server.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above. `dedup-import` detects the format with `is_legacy` and prints `Imported N new + M replaced (K kept, newer in DB) decision(s) from <path>`. For a legacy file it adds `; D member(s) dropped (no live track)`. The `dedup` summary reports `manual_links`, plus superseded and dead when non-zero.
- [ ] **Step 4: Run** `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(dedup): dedup, MCP edits, card and mirror run on pair decisions`.

### Task 7: The main file comes from the canon's core

**Files:**
- Modify:
  - `airdrome/models.py`: add to `TrackGroup` and change `recompute_main`;
  - `airdrome/library/organize.py`: `transfer_track` and `split_main_copies`;
  - `airdrome/library/scan.py`: `_verdict`.
- Test: `tests/test_recompute_mains.py`, `tests/test_organize.py`, `tests/test_import_classify.py`

**Interfaces:**
- Consumes: `Track.canon_folded`, written by Task 6's `apply_plan`.
- Produces:
  - `TrackGroup.core_members -> list[Track]` (property): the canon plus twins with `canon_folded` False.
  - `TrackGroup.main_candidates() -> list[TrackFile]`: the core members' files, or every member's files when the core has none.
  - `recompute_main` picks `select_main_file(main_candidates())` and still clears `is_main` on every group file.
  - In `organize`, the "best" in `transfer_track`, including the upgrade-swap check, and the main in `split_main_copies` both come from `main_candidates()`. The other files are still placed as copies.
  - In `classify`, `_verdict` compares `best` against the max kbps of `main_candidates()`.

- [ ] **Step 1: Write the failing tests**
  - `test_main_stays_with_core_when_folded_file_is_better`: canon `c` (128 kbps) with its auto twin, and a manually folded `f` (320 kbps, other recording id). After `auto_deduplicate`, `c`'s file has `is_main`, and `f.canon_folded is True`.
  - `test_main_falls_back_when_core_has_no_file`: the canon has no files and the folded twin has one. That file is main.
  - `test_group_without_fold_picks_best_anywhere`: unchanged behaviour, with `canon_folded` False everywhere.
  - `test_organize_does_not_swap_in_folded_upgrade`: a placed core main and an unplaced better folded file. `organize` keeps the core file in `Library/` and puts the folded file in `Copies/`.
  - `test_classify_upgrade_compares_core_files`: a new copy beating the core's best is `upgrade`. One that beats only the folded file is `have`.
- [ ] **Step 2: Run** `uv run pytest tests/test_recompute_mains.py tests/test_organize.py tests/test_import_classify.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(dedup): a manually folded group plays its canon's own recording`.

### Task 8: Remove the TUI

**Files:**
- Delete: `airdrome/normalize/dedup/manual.py`, `airdrome/normalize/dedup/tui.py`, `tests/test_dedup_manual.py`.
- Modify:
  - `airdrome/normalize/dedup/persistence.py`: delete `save_confirmed_groups`, `load_confirmed_groups` and the `Page` import;
  - `airdrome/normalize/dedup/__init__.py`: drop `Deduplicator` and `DeduplicatorUI`;
  - `airdrome/terminal/pipeline.py`: drop `--review/-r` and `--match`, and print with the new helper;
  - `tests/factories.py`: drop `make_page`;
  - `tests/test_dedup_integration.py` and `tests/test_dedup_recording.py`: delete only their `Deduplicator` cases.
- Create: `airdrome/terminal/tables.py`
- Test: `tests/test_cli_defaults.py`

**Interfaces:**
- Produces: `dedup_table(key: str, tracks: list[Track], canons: list[int | None]) -> rich.table.Table` in `airdrome/terminal/tables.py`. It is `DeduplicatorUI.compose_table` plus its row builder, moved; the columns and styles are unchanged.
- No dependency is dropped: `rich` is used by `airdrome/console.py`. Check `pyproject.toml` anyway and drop anything left that only `tui.py` / `manual.py` imported.

- [ ] **Step 1: Write the failing tests**
  - `test_dedup_has_no_review_option`: `CliRunner().invoke(app, ["dedup", "--review"])` exits with a usage error.
  - `test_dedup_table_lists_each_track`: a 2-track group renders a `Table` with 2 rows and a `Canon ID` column.
- [ ] **Step 2: Run** `uv run pytest tests/test_cli_defaults.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the deletions and the helper. `grep -rn "Deduplicator\|confirmed_groups\|manual import" airdrome tests` comes back empty.
- [ ] **Step 4: Run** `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(dedup): remove the TUI; dedup review goes through MCP`.

### Task 9: Drop the old group tables and their remaining consumers

**Files:**
- Modify:
  - `airdrome/models.py`: delete `DedupGroup` and `DedupGroupMember`. Keep `Track.duplicate_hash`, because Task 2's `test_legacy_hash_matches_property` reads it;
  - `airdrome/normalize/dedup/persistence.py`: delete `stored_canons`, `stored_cannot_links` and `_load_stored_index`;
  - `airdrome/library/ladder.py`: delete `stored_member_hashes`; drop `backfill`'s and `upsert_track`'s `stored_hashes` parameter and `LadderContext`'s field;
  - `airdrome/library/unify.py`: the call site at `:111`;
  - `airdrome/terminal/status.py`;
  - `scripts/identity_acceptance.py`;
  - `airdrome/normalize/dedup/store.py`: add `decision_counts`;
  - `tests/factories.py`: delete `make_dedup_group`;
  - the old stability tests in `tests/test_ladder.py`, `tests/test_import_classify.py`, `tests/test_apple_ingest.py` and `tests/test_dedup_persistence.py`: delete or rewrite them as below.
- Replace: the squashed migration (regenerate again)
- Test: `tests/test_status_counts.py`, `tests/test_ladder.py`

**Interfaces:**
- Consumes: Task 5's `store.resolve_library`, `load_pairs` and `load_canons`.
- Produces:
  - `decision_counts(s: Session) -> dict[str, int]`, with keys `same`, `different`, `canons`, `superseded` and `dead`. `dead` counts pairs plus canons.
  - `pipeline_counts()["dedup"]` becomes `{"twins", "groups", "same", "different", "canons", "superseded", "dead"}`, replacing `confirmed_groups`. Keep the `has_table` guard style: no decision table means those keys are 0.
  - `status` renders one row each.
  - In `identity_acceptance`, `_partial_stored_groups` becomes `_decision_liveness(engine) -> tuple[int, int]`, returning (live, dead) pair decisions.

- [ ] **Step 1: Write the failing tests**
  - `test_status_counts_decisions`: one accepted `same`, one superseded `different`, one dead pair and one canon give `same == 1`, `different == 1`, `superseded == 1`, `dead == 1` and `canons == 1`.
  - `test_backfill_fills_hashed_fields_of_decided_track`: a track named in a `same` pair with `year=None`. `land` backfills the year, and the next `auto_deduplicate` still links the pair.
- [ ] **Step 2: Run** `uv run pytest tests/test_status_counts.py tests/test_ladder.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** `grep -rn "DedupGroup\|stored_member_hashes\|stored_canons\|stored_cannot_links\|confirmed_groups" airdrome tests scripts` matches only `legacy.py` (its Core tables) and its tests.
- [ ] **Step 4: Regenerate the migration.** The new file has no `dedupgroup*` tables. Run `uv run pytest`. Expected: PASS.
- [ ] **Step 5: Commit** `refactor(dedup): drop stored groups; status counts decisions`.

### Task 10: Docs

**Files:**
- Modify:
  - `CLAUDE.md`:
    - the `dedup` and `dedup-import` command bullets;
    - Data flow step 4;
    - Key models (`PairDecision`, `CanonChoice`, `Track.canon_folded`);
    - the Identity ladder bullet on stored "not duplicates", which becomes a pair `different` and the newest-wins rule;
    - *Dedup group mirror*, renamed *Dedup decision mirror*: v2 shape, legacy file raises on restore, upsert rule;
    - *Navidrome integration*: the main comes from the core;
    - the *Session lifecycle* exception list (the interactive `Deduplicator` is gone);
  - `README.md`: the `dedup` flags, `dedup-import` formats, and the rollout note about the migration script;
  - `docs/design/identity-ladder.md`;
  - `docs/superpowers/specs/2026-10-18-airdrome-mcp-design.md`: wherever it describes stored groups, the card's `stored_groups`, or the refusals;
  - model and module docstrings still naming groups;
  - `ROADMAP.md`: remove the 🔨 pair-decision item (its current state is now in `CLAUDE.md`), and drop the "needs the refactor" note from the duplicate-review item.
- Create: `docs/design/dedup-decisions.md`:
  - why pairs;
  - natural key versus hash or id;
  - newest wins;
  - why manual crosses recording ids;
  - the core rule;
  - rejected: an invariant patch on groups (closure plus guards);
  - rejected: a hash-keyed pair.

- [ ] **Step 1:** Make the edits. `grep -rn "DedupGroup\|dedup -r\|--review\|confirmed group" CLAUDE.md README.md docs ROADMAP.md` matches only historical plans and specs under `docs/superpowers/` and the new design doc's *rejected* section.
- [ ] **Step 2:** `uv run pytest` and `ruff check .` (no code changed). Expected: PASS.
- [ ] **Step 3: Commit** `docs: pair decisions replace stored dedup groups`, then `git push`.

## Execution rulings (controller, 2026-10-18..19)

Every decision taken during subagent-driven execution, with what it costs if wrong. Owner decisions are marked.

- F6 keep live member whose canon hash is dead, emit `different` vs differently-resolving members, count separately — spec drops only members with no live track — migration loses "not duplicates" decisions if wrong
- F15 T8 CLI test uses stub_session — root callback would migrate live DB_DSN — a test touching live DB
- F1 add airdrome/terminal/mcp.py (seed_dedup_groups import) to T6 — else import error — trivial
- F2 T8 also deletes test_save_*/test_load_* in tests/test_dedup_persistence.py — they import deleted names — trivial
- F3 drop confirmed_groups from T8 grep — status.py keeps it until T9 — trivial
- F4 add conf.py to T9, widen grep allow-list — comment hit — trivial
- F5 stash_mirror_snapshot moves into store.py; legacy.write_migration imports write_mirror lazily — breaks import cycles — rework of module layout
- F11 edit_scope queries current groups by canon_id inside auto.py; tests take scope from plan built after save — edits.py imports auto.py — scope slightly different shape
- F9 write_mirror annotation widened to dict[str, object] only — v2 doc shape — trivial
- F7 convert_group skips member whose canon hash == own hash (no pair(k,k)); add test — occurs in live data — crash in migration if wrong
- F8 add_pair default timestamps from fixed past base 2020-01-01Z — MCP edits must beat fixtures — flaky tests
- F10 T6 choose canon first, then core; manual components loaded order_by(canon_order) — step dependency — wrong canon/core
- F12 status same/different counts are non-dead rows by kind; test states dead pair's kind — ambiguity — cosmetic
- F16 split_main_copies gets optional `candidates` param — needs the core — API shape
- F22 load_pairs/load_canons sorted in Python — collation vs Python order — round-trip test flake
- F13 two tests (get_or_create null row; backfill hashed fields) kept as regression guards, marked may-already-pass — cannot go RED — none
- F14 repro test uses explicit timestamps; classify "have" test built as folded file > new copy > core — tests must fail on old code — weak tests
- F20 legacy_hash duplicates duplicate_hash formula on purpose, guarded by parity test, docstring says so — legacy reader must not depend on ORM — drift caught by test
- F17 T10 runs full pytest+ruff — docstrings changed — none
- F18 T5/T9 also run `alembic upgrade head` on an empty scratch DB — create_all hides migration bugs — broken migration at rollout
- F19 accept full plan_dedup per MCP edit; time one edit on live-sized DB before rollout — spec allows — slow edits
- T2 repeated member hash — union semantics: same(k,c) for every non-null canon seen for the hash (self pairs skipped), different only between hashes whose canon sets don't overlap, disagreeing hashes counted; order-independent — old last-wins depended on row order, which is the bug this plan kills — if wrong, a few MCP groups convert to extra 'same' links
- T5 plan-mandated distinct-album fix lost default-flag coverage — use case-variant raw keys (equal _norm) and restore original call args — spec wants tests to keep testing grouping defaults — if wrong, a few tests rewritten twice
- T6 fingerprint representative must not read is_main — deterministic over fingerprinted files (highest bitrate, then lowest id) — global constraint: edit == following dedup, dedup idempotent — if wrong, a different file's fingerprint represents a track (slightly different fp grouping)
- T6 future-dated imported decided_at clamped to now at import; edit stamps max(now, stored+1µs) — skewed clocks must not outrank the owner's edits — if wrong, an imported decision loses to a later edit it should have beaten (can't happen with honest clocks)
- T6 legacy re-import re-dating groups to now is accepted (plan-mandated) — legacy import is a one-time entrance; the live migration uses the script with past spaced timestamps — if wrong, re-importing an old archive overrides newer MCP edits
- T7 transfer_track pending-copies branch narrowed to files outside main_candidates (folded members) — brief's test needs only that; widening changes disk behaviour for every placed group and adds an abort path — if wrong, ordinary stranded twin copies stay unplaced as before (status quo)
- T8 review Important items routed instead of a fix round — dead stored_canons/stored_cannot_links/_load_stored_index + 4 tests + __init__ re-export + ladder.py docstring go to Task 9 (they read DedupGroup, which Task 9 drops); tables.py `str(t.year) or ""` bug fixed in Task 9; stale CLAUDE.md lines (expire_on_commit/interactive deduplicator, TUI reset rationale, stash_mirror_snapshot location), README dangling comment and docs/design/identity-ladder.md --review refs go to Task 10 — all land before merge, reviewer approved the task — if wrong, nothing ships broken; one extra hop
- migrated canon choices may shift after rebuild (spec rule: canon chosen only where it differed from canon_order) — accepted per spec — if wrong, a few groups show a different canon until the owner picks one via MCP
- parked — README step 2 dropdb not chained to a verified pg_dump; repeated step 0 refused when OUT exists so a stale rehearsal file could be mv'd; refusal text doesn't flag dedup-import of the .bak as lossy last resort; README hardcodes DB name `airdrome`; upgrade_to_head shows a raw alembic error — the controller runs the rollout by hand with these checks (verified dump, fresh dated OUT, DSN check) — if wrong, a future reader following README literally could drop the old DB without a usable dump (decisions still survive in the v2 file and .bak)
- (owner) Release identity is out of scope here; ROADMAP item added, release-time spelling wins.
