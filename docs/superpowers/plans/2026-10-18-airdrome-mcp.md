# Airdrome MCP Server v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `airdrome mcp`, a local FastMCP server that shows the library (`wanted`, track card, unmatched aliases, status) and makes three point edits (wanted/skip verdict, same/different track, bind/reject alias) that survive `land` and `dedup`.

**Architecture:** Domain logic goes in plain modules that take `s: Session` and only flush:
- `airdrome/wanted/` for verdicts and the two lists;
- `airdrome/library/edits.py` and `airdrome/library/card.py`;
- a refactored `airdrome/terminal/status.py`.

`airdrome/mcpserver/server.py` wraps them as FastMCP tools. Each call opens and commits its own session. `airdrome/terminal/mcp.py` is the CLI entry, and it bypasses the root callback's session the way `status` does.

**Tech Stack:** Python 3.14, SQLAlchemy 2, PostgreSQL + pg_trgm, alembic, Typer, FastMCP (HTTP transport), pytest against the compose Postgres.

**Spec:** `docs/superpowers/specs/2026-10-18-airdrome-mcp-design.md`

## Global Constraints

- Every function and method gets a one-line docstring. Line length is 110. Ruff rules include `ANN`, so every parameter and return is annotated. No bare `Any` outside `models.py` / `cloud/sources.py`.
- Domain functions take `s: Session` and call `s.flush()`, never `s.commit()`. Only `mcpserver.session.tool_session` commits.
- The token is `AIRDROME_MCP_TOKEN` (`SecretStr | None` in `conf.py`). Never print or log it. Disable the uvicorn access log, because it prints the request path, and the path contains the token.
- The server listens on `127.0.0.1` only. The default port is `8765` (`AIRDROME_MCP_PORT`). The MCP path is `/mcp/<token>`.
- The token never enters git. Register the server with `claude mcp add --transport http --scope local airdrome http://127.0.0.1:8765/mcp/<token>`, never in the tracked `.mcp.json`.
- `airdrome mcp` never migrates. If the DB is not at alembic head, it exits non-zero with a message.
- Invariants every edit keeps:
  - `canon_id` is terminal;
  - each group has exactly one `is_main` file (`recompute_main_files` after a canon change);
  - a stored all-NULL group is a cannot-link;
  - a group has one recording id.
- Every list tool is paged: `limit` defaults to 50, capped at 200. `cursor` is the stringified offset, and the response carries `next_cursor` (None at the end).
- Rows resolve to the canonical track (one hop). Plays are summed over the whole group.
- Library = tracks with an `APPLE_XML` `SourceTrack` (and `SPOTIFY` once the importer lands). `APPLE_MS` does NOT count.
- Verdict precedence: `skip` beats `wanted`.
- No `Co-Authored-By` trailer on any commit. Commit after each task, push at the end.

## Review Focus

1. **The token leaking through logs.** A request must not write the path to stdout or stderr. Test: `test_server_logs_never_contain_token` (Task 8).
2. **`different(A, B)` where B is the canon of A's group.** The rest of the group keeps a new canon (the `canon_order` pick), and B becomes a lone root. Test: `test_different_detaches_canon` (Task 3).
3. **Repeat or redundant edits.** `same` on two tracks already in one group, `bind` of an alias to the track it already has, and setting a verdict twice are all no-ops with the same result. Tests: `test_same_is_idempotent`, `test_bind_is_idempotent`, `test_set_verdict_twice_keeps_one_row` (Tasks 3, 4, 5).
4. **An unmatched alias with no artist.** It groups under an empty artist key, never crashes, and is not merged with an artist-bearing row of the same title. Test: `test_unmatched_rows_blank_artist` (Task 6).
5. **A verdict on a twin when the canon changes later.** The group still shows that verdict. Test: `test_verdict_follows_group_after_merge` (Task 5).

---

### Task 1: Schema — verdict table, alias reject, schema-at-head check

**Files:**
- Modify: `airdrome/enums.py` (add `Verdict`)
- Modify: `airdrome/models.py` (add `WantedVerdict`)
- Modify: `airdrome/scrobbles/match_aliases.py` (`do_match_aliases` skips rejected aliases)
- Modify: `airdrome/migrations.py` (add `is_at_head`)
- Replace: the single migration in `alembic/versions/` (regenerate per CLAUDE.md *Migrations*)
- Test: `tests/test_wanted_schema.py`, `tests/test_match_aliases.py`

**Interfaces:**
- Produces:
  - `Verdict(StrEnum)`: `WANTED = "wanted"`, `SKIP = "skip"`.
  - `WantedVerdict` (table `wantedverdict`):
    - `id`;
    - `track_id: int | None` (FK `track.id`, `ondelete="CASCADE"`, unique);
    - `alias_id: int | None` (FK `trackalias.id`, `ondelete="CASCADE"`, unique);
    - `verdict: Verdict` (`native_enum=False`);
    - `note: str | None`;
    - `decided_at: AwareDatetimeDefNow`;
    - a `CheckConstraint` that exactly one of `track_id` / `alias_id` is set.
  - A rejected alias is `track_id IS NULL AND match_method = BindMethod.MANUAL`.
  - `is_at_head() -> bool`.

- [ ] **Step 1: Write the failing tests**
  - `test_verdict_requires_exactly_one_target`: inserting a `WantedVerdict` with both ids set, or with neither, raises `IntegrityError` on flush.
  - `test_rejected_alias_is_not_rematched` in `tests/test_match_aliases.py`: an alias with an exact-tag track available and `match_method=MANUAL, track_id=None` is still unbound after `do_match_aliases(s)`, and the function returns `(0, 0)` for it, because it is not even counted.
- [ ] **Step 2: Run** `uv run pytest tests/test_wanted_schema.py tests/test_match_aliases.py -v`. Expected: FAIL (no `WantedVerdict`; the alias gets bound).
- [ ] **Step 3: Implement.**
  - Add the model and the enum.
  - Change the `do_match_aliases` query to `TrackAlias.track_id.is_(None)` AND `or_(TrackAlias.match_method.is_(None), TrackAlias.match_method != BindMethod.MANUAL)`.
  - In `is_at_head`, compare `MigrationContext.configure(conn).get_current_revision()` with `ScriptDirectory.from_config(_build_config()).get_current_head()`.
  - Regenerate the migration: delete the old file, then `uv run alembic revision --autogenerate -m "initial schema"` against an empty DB. Keep the `CREATE EXTENSION pg_trgm` line the old one had.
- [ ] **Step 4: Run** the same tests. Expected: PASS. Then run `uv run pytest`: everything passes.
- [ ] **Step 5: Commit** `feat(schema): wanted verdict table, manual alias reject, is_at_head`.

### Task 2: Status counts as data

**Files:**
- Modify: `airdrome/terminal/status.py`
- Test: `tests/test_status_counts.py`

**Interfaces:**
- Produces: `pipeline_counts(s: Session) -> dict[str, dict[str, int]]`, with sections:
  - `imported`: `source_tracks`, `source_playlists`, `scrobbles`, plus `source_tracks.<provider>` keys;
  - `canonical`: `tracks`, `aliases`, `aliases_matched`, `plays`, `playlists`;
  - `files`: `files`, `bound`, `organized`;
  - `dedup`: `twins`, `groups`, `confirmed_groups`;
  - `synced`: `playlist_links`;
  - `wanted`: `verdict_wanted`, `verdict_skip`.

  `_print_pipeline` renders from it, so the output stays unchanged.

- [ ] **Step 1: Write the failing test** `test_pipeline_counts_shape`. Use factories to make 2 tracks, 1 file bound to one of them, 1 alias matched and 1 not. Assert `counts["canonical"]["tracks"] == 2`, `counts["files"]["bound"] == 1`, and `counts["canonical"]["aliases_matched"] == 1`.
- [ ] **Step 2: Run it.** Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `pipeline_counts`, and rewrite `_print_pipeline` to read from it.
- [ ] **Step 4: Run** the test, then `uv run airdrome status` against the live DB. Expected: PASS, and the same tables as before plus nothing missing.
- [ ] **Step 5: Commit** `refactor(status): pipeline_counts returns data`.

### Task 3: Same / different track edits

**Files:**
- Create: `airdrome/library/edits.py`
- Test: `tests/test_library_edits.py`

**Interfaces:**
- Consumes: `canon_order`, `recompute_main_files`, `stash_mirror_snapshot`, `DedupGroup`/`DedupGroupMember`, `Track.duplicate_hash`.
- Produces:
  - `group_of(s: Session, track_id: int) -> list[Track]`: the canon and all its twins, canon first.
  - `mark_same(s: Session, a_id: int, b_id: int) -> Track`: returns the group's canon.
  - `mark_different(s: Session, a_id: int, b_id: int) -> None`.
  - `EditError(ValueError)`: raised for an unknown id, or for `a_id == b_id`.

How both edits work, since the signatures don't determine it:
- Build the **affected set** as the closure of:
  - `group_of(a)` ∪ `group_of(b)`;
  - every member track of any stored `DedupGroup` whose member hashes intersect the set's hashes (repeat until stable).
- Compute the target canon per track:
  - **`mark_same`:** every track goes to one root. The root is the first of the affected tracks that are in `group_of(a) ∪ group_of(b)`, in `canon_order()` order. Tracks pulled in only through a stored group keep their current canon (re-pointed to the new root if their canon was a member of a merged group).
  - **`mark_different`:**
    - if a and b already have different roots, nothing changes in the canon map;
    - if they share a root R, then b's track (or, when b is R, the track `b_id`) becomes a lone root;
    - if b was R, the remaining members get a new root, the first in `canon_order()`.
- Delete the stored groups that intersect. Write one `DedupGroup` (label `f"mcp:{a_id}:{b_id}"`) whose members are the affected hashes, with `canon_hash` = the root's hash, or NULL for a root. Because every member resolves to its stored canon, `stored_cannot_links` keeps different roots apart and `apply_manual_overrides` reproduces the map.
- Apply the map to `Track.canon_id`. Linked tracks get `canon_method=MANUAL, canon_confidence=1.0`; roots get None for both. Then call `recompute_main_files(s)` and `stash_mirror_snapshot(s)`.

- [ ] **Step 1: Write failing tests**, each with factory tracks:
  - `test_same_merges_two_groups`: A has twin A2, B is alone. `mark_same(A, B)` → A2 and B have `canon_id == A.id` (A is first in `canon_order` by `date_added`), and A has `canon_id is None`.
  - `test_same_is_idempotent`: calling it twice → still one stored group, same canons.
  - `test_different_detaches_canon`: A is the canon of A2 and A3. `mark_different(A2, A)` → A is a lone root, and A2 and A3 share a root that is not A.
  - `test_different_survives_dedup`: after `mark_different` on a pair whose titles and artists are identical, `auto_deduplicate(s)` leaves them apart.
  - `test_same_survives_dedup`: after `mark_same` on two different titles, `auto_deduplicate(s)` keeps them together.
  - `test_same_rejects_self`: `mark_same(A, A)` raises `EditError`.
  - `test_same_recomputes_main`: A and B each have a file with `is_main=True` → afterwards exactly one `is_main` remains across the group, the higher-bitrate one.
- [ ] **Step 2: Run** `uv run pytest tests/test_library_edits.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the functions above in `airdrome/library/edits.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then run `uv run pytest`: all pass.
- [ ] **Step 5: Commit** `feat(edits): same/different track as stored dedup groups`.

### Task 4: Alias bind / reject

**Files:**
- Modify: `airdrome/library/edits.py`
- Test: `tests/test_library_edits.py`

**Interfaces:**
- Produces:
  - `bind_aliases(s: Session, alias_ids: list[int], track_id: int) -> Track`: binds to the track's canon and returns it.
  - `reject_aliases(s: Session, alias_ids: list[int]) -> None`.

Behaviour:
- **Bind** sets `track_id = canon.id`, `match_method=MANUAL`, `match_confidence=1.0`. For each scrobble of the alias, it upserts a `TrackPlay` keyed by `source_scrobble_id`, the same shape as `do_copy_plays` (reuse its loop body by extracting `copy_alias_plays(s: Session, alias: TrackAlias) -> int` in `scrobbles/copy_plays.py`; `do_copy_plays` then calls it).
- **Reject** sets `track_id=None`, `match_method=MANUAL`, `match_confidence=None`, and deletes the `TrackPlay` rows whose `source_scrobble_id` belongs to the alias's scrobbles.
- An unknown id raises `EditError`.

- [ ] **Step 1: Write failing tests:**
  - `test_bind_moves_plays`: an alias with 3 scrobbles is bound by fuzzy to X. `bind_aliases([alias], Y)` → 3 `TrackPlay` rows on Y's canon, none on X, and `match_method == MANUAL`.
  - `test_bind_is_idempotent`: bind twice → still 3 plays.
  - `test_reject_removes_plays_and_survives_land`: reject → 0 plays for those scrobbles. Then `do_match_aliases(s)` and `do_copy_plays(s)` → the alias is still unbound and still has 0 plays.
  - `test_manual_bind_survives_match`: bind, then `do_match_aliases(s)` → unchanged.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `copy_alias_plays`, `bind_aliases`, `reject_aliases`.
- [ ] **Step 4: Run** `uv run pytest tests/test_library_edits.py tests/test_match_aliases.py -v`, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(edits): manual alias bind/reject with play sync`.

### Task 5: Verdicts

**Files:**
- Create: `airdrome/wanted/__init__.py`, `airdrome/wanted/verdicts.py`
- Test: `tests/test_wanted_verdicts.py`

**Interfaces:**
- Produces:
  - `set_track_verdict(s: Session, track_id: int, verdict: Verdict, note: str | None = None) -> WantedVerdict`: stored on the canon of `track_id`, upserting by `track_id`.
  - `set_alias_verdict(s: Session, alias_ids: list[int], verdict: Verdict, note: str | None = None) -> list[WantedVerdict]`: one row per alias, upserted.
  - `clear_verdict(s: Session, *, track_id: int | None = None, alias_ids: list[int] | None = None) -> int`: returns the number of rows deleted.
  - `group_verdicts(s: Session) -> dict[int, Verdict]`: canon root id → effective verdict. Every verdict on any member track, plus every verdict on an alias bound to any member, is folded in, and `SKIP` wins.
  - `alias_verdicts(s: Session) -> dict[int, Verdict]`: alias id → verdict, for unbound aliases only.

- [ ] **Step 1: Write failing tests:**
  - `test_set_verdict_twice_keeps_one_row`.
  - `test_verdict_on_twin_stored_on_canon`.
  - `test_verdict_follows_group_after_merge`: a verdict on B, then `mark_same(A, B)` with A as canon → `group_verdicts(s)[A.id] == WANTED`.
  - `test_skip_wins`: `WANTED` on A and `SKIP` on B, then `mark_same` → `SKIP`.
  - `test_alias_verdict_moves_on_bind`: `SKIP` on an unbound alias, then `bind_aliases([alias], T)` → `group_verdicts(s)[T.id] == SKIP`, and the alias no longer appears in `alias_verdicts`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(wanted): verdicts with group resolution`.

### Task 6: The two `wanted` lists

**Files:**
- Create: `airdrome/wanted/lists.py`
- Test: `tests/test_wanted_lists.py`

**Interfaces:**
- Consumes: `group_verdicts`, `alias_verdicts`.
- Produces:
  - `LIBRARY_PROVIDERS: frozenset[Source] = frozenset({Source.APPLE_XML, Source.SPOTIFY})`.
  - `@dataclass(frozen=True) class WantedRow`:
    - `kind: Literal["track", "unmatched"]`;
    - `track_id: int | None`, `alias_ids: list[int]`;
    - `title: str | None`, `artist: str | None`, `album: str | None`;
    - `plays: int`;
    - `first_played: datetime | None`, `last_played: datetime | None`;
    - `months: int`: distinct year-months with a play;
    - `playlists: int`, `loved: bool`, `rating: int | None`;
    - `sources: list[str]`: providers of the group's `SourceTrack`s;
    - `verdict: Verdict | None`;
    - `similar_with_file: dict | None`, of the form `{"track_id", "title", "artist", "score"}`.
  - `@dataclass(frozen=True) class Page`:
    - `rows: list[WantedRow]`;
    - `total: int`;
    - `next_cursor: str | None`.
  - `wanted_library(s: Session, cursor: str | None = None, limit: int = 50, verdict: Verdict | None = None, query: str | None = None) -> Page`.
  - `wanted_scrobbled(s: Session, cursor: str | None = None, limit: int = 50, verdict: Verdict | None = None, query: str | None = None, include_unmatched: bool = True) -> Page`.
  - `similar_with_file(s: Session, title_norm: str, artist_norm: str, exclude_root: int | None) -> dict | None`.

Rules:
- **A file-less group** is a canon root none of whose members has a `TrackFile`.
- **`wanted_library`:**
  - includes groups where some member has a `SourceTrack` with a provider in `LIBRARY_PROVIDERS`;
  - sorts by the root's `date_added`, oldest first.
- **`wanted_scrobbled`:**
  - includes file-less groups with ≥ 1 `TrackPlay` across the group;
  - if `include_unmatched`, also includes rows of unbound aliases (rejected ones included) that have ≥ 1 scrobble, collapsed on `(artist_norm, title_norm)`. Plays come from `TrackAliasScrobble`, and `title`/`artist` from the alias with the most scrobbles;
  - sorts by `plays` desc, then by title.
- **Both lists:**
  - drop rows whose verdict is `SKIP`, unless `verdict == SKIP` is requested;
  - `verdict` filters to that verdict exactly;
  - `query` is a case-insensitive substring match on title_norm or artist_norm, normalized with `normalize_name`;
  - `limit` is capped at 200;
  - the cursor is an int offset; anything that is not a non-negative int raises `ValueError("bad cursor")`.
- **`similar_with_file`** runs only for the rows of the returned page. It takes the top `Track` by `similarity(title_norm) * 0.5 + similarity(artist_norm) * 0.5` among tracks whose group has a file, excluding `exclude_root`'s group, and returns it when the score is ≥ 0.6. Unmatched rows pass `exclude_root=None`.
- An unmatched row's `months` / `first_played` / `last_played` come from the scrobble dates. Its `playlists` is 0 and `loved` is False.

- [ ] **Step 1: Write failing tests** (factories plus `SourceTrack` and `TrackAliasScrobble` rows):
  - `test_library_excludes_apple_ms_only`: one group with only an `APPLE_MS` source and one with `APPLE_XML`, both file-less → only the `APPLE_XML` one is listed.
  - `test_library_excludes_groups_with_file_on_twin`.
  - `test_scrobbled_sorted_by_plays`: groups with 5 and 2 plays plus an unmatched alias with 3 → order 5, 3, 2, and the middle row has `kind == "unmatched"`.
  - `test_unmatched_rows_collapse_aliases`: two aliases with the same artist/title on different albums → one row, `alias_ids` holds both, plays are summed.
  - `test_unmatched_rows_blank_artist`: an alias with no artist and an alias with artist "x", same title → two rows.
  - `test_skip_hidden_by_default`.
  - `test_paging`: 3 rows with `limit=2` → `next_cursor == "2"`; a second call with that cursor → 1 row and `next_cursor is None`.
  - `test_similar_with_file_flags_ghost`: a file-less "Song (Remastered)" by "Band" and a filed "Song" by "Band" → the row's `similar_with_file["track_id"]` is the filed track.
  - `test_months_counts_distinct_months`: plays on 2024-01-03, 2024-01-20 and 2024-03-01 → `months == 2`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Use one aggregate SQL per list for the track rows: group by `coalesce(canon_id, id)`, with `count(*)`, `min`/`max(played_at)`, and `count(distinct date_trunc('month', played_at))`.
  - Use one aggregate SQL for the unmatched rows.
  - Merge, sort and slice in Python.
- [ ] **Step 4: Run** the tests. Then time both functions against the live DB (read-only):

  ```
  uv run python -c "import time;from sqlalchemy.orm import Session;from airdrome.models import engine;from airdrome.wanted.lists import wanted_scrobbled as w;s=Session(engine);t=time.time();p=w(s);print(p.total,round(time.time()-t,2))"
  ```

  Expected: PASS, and each call takes < 3 s. If it is slower, add an index (the plan's only allowed schema change after Task 1). That means regenerating the migration again.
- [ ] **Step 5: Commit** `feat(wanted): library and scrobbled lists with signals`.

### Task 7: Track card and unmatched aliases

**Files:**
- Create: `airdrome/library/card.py`
- Test: `tests/test_card.py`

**Interfaces:**
- Produces:
  - `track_card(s: Session, track_id: int) -> dict`. It takes any member and returns the whole group, as JSON-serializable data:
    - `canon`;
    - `members`: each with `id`, the tags, `canon_method`, `canon_confidence`, `sources[{provider, source_id}]`, `files[{id, path, bitrate, is_main, bind_method, bind_confidence, recording_mbid}]`;
    - `aliases`: `{id, title, artist, album, match_method, match_confidence, scrobbles}`, capped at 50 by scrobbles, plus `aliases_total`;
    - `plays`, `playlists[{id, name}]`;
    - `stored_group`: `{label, members: [{track_ids, canon_track_id}]}` or None;
    - `verdict`.
  - `unmatched_aliases(s: Session, cursor: str | None = None, limit: int = 50, query: str | None = None) -> dict`. It returns `{rows, total, next_cursor}`, where each row is `{alias_ids, title, artist, album, scrobbles, rejected, candidates: [{track_id, title, artist, album, score}]}`. Rows are collapsed exactly like Task 6's unmatched rows, so share a helper with `lists.py` rather than copying it. Candidates are the top 5 from a variant of `find_best_match` that returns the top N. Add `find_matches(...) -> list[tuple[Track, float]]` in `match.py` and make `find_best_match` take its first element.

- [ ] **Step 1: Write failing tests:**
  - `test_card_from_twin_shows_whole_group`;
  - `test_card_unknown_id_raises` (`EditError`);
  - `test_unmatched_candidates_ranked`: an alias "song"/"band" with tracks "song"/"band" and "song"/"other" → the first candidate is "band" and scores are non-increasing;
  - `test_find_best_match_unchanged`: existing behaviour equals `find_matches(...)[0]`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and `tests/test_match_aliases.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(card): track card and unmatched aliases with candidates`.

### Task 8: FastMCP server and `airdrome mcp`

**Files:**
- Modify: `pyproject.toml` (`uv add fastmcp`; keep whatever version resolves)
- Modify: `airdrome/conf.py`:
  - `airdrome_mcp_token: SecretStr | None = None` (env `AIRDROME_MCP_TOKEN`; Settings has no env prefix);
  - `airdrome_mcp_port: int = 8765`.
- Create: `airdrome/mcpserver/__init__.py`, `airdrome/mcpserver/session.py`, `airdrome/mcpserver/server.py`
- Create: `airdrome/terminal/mcp.py`. Modify `airdrome/terminal/app.py`: register the command, and early-return in the callback when `ctx.invoked_subcommand == "mcp"`.
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces:
  - `tool_session(engine: Engine) -> ContextManager[Session]`: `Session(engine, expire_on_commit=False)` plus `install_mirror(session, settings.duplicates_file)`. It commits on normal exit, rolls back and re-raises on an exception, and always closes.
  - `build_server(engine: Engine) -> FastMCP`: the server named `"airdrome"`. Each tool is a thin wrapper: it opens a `tool_session`, calls the domain function, and returns JSON-serializable data (dataclasses via `asdict`). An `EditError` or `ValueError` becomes a tool error with its message. Tools:

    | tool | domain call |
    |---|---|
    | `wanted_library(cursor, limit, verdict, query)` | Task 6 |
    | `wanted_scrobbled(cursor, limit, verdict, query, include_unmatched)` | Task 6 |
    | `track_card(track_id)` | Task 7 |
    | `unmatched_aliases(cursor, limit, query)` | Task 7 |
    | `library_status()` | Task 2 `pipeline_counts` |
    | `set_verdict(verdict, track_id=None, alias_ids=None, note=None)` | Task 5; exactly one target, else a tool error |
    | `clear_verdict(track_id=None, alias_ids=None)` | Task 5 |
    | `mark_same_track(track_id_a, track_id_b)` | Task 3; returns `track_card` of the result |
    | `mark_different_tracks(track_id_a, track_id_b)` | Task 3; returns both cards |
    | `bind_aliases(alias_ids, track_id)` | Task 4; returns `track_card` |
    | `reject_aliases(alias_ids)` | Task 4 |

    Each tool's docstring is what the AI reads, so state the semantics in it: for example, that part 1 = the owner's library (iTunes XML, later Spotify liked + own playlists), that `skip` hides a row, and that edits survive `land`/`dedup`.
  - `airdrome mcp` (Typer command, no options):
    1. If `settings.airdrome_mcp_token` is None, exit 1 with the message "set AIRDROME_MCP_TOKEN in .env".
    2. If `not is_at_head()`, exit 1 with "schema is not at head — run any write command (e.g. `airdrome land -n`) first".
    3. Print `airdrome MCP on http://127.0.0.1:<port>/mcp/<redacted>`.
    4. Call `server.run(transport="http", host="127.0.0.1", port=port, path=f"/mcp/{token}", ...)`, with the uvicorn access log off. Check the installed FastMCP's `run` signature for how to pass `access_log=False` (its `uvicorn_config`, or run uvicorn on `server.http_app(path=...)` directly).

- [ ] **Step 1: Write failing tests** using FastMCP's in-memory `Client(build_server(test_engine))`. Use the `test_engine` fixture with real commits, and clean up the rows they create at teardown.
  - `test_tools_listed`: the tool names equal the 11 in the table.
  - `test_set_verdict_roundtrip`: call `set_verdict` on a track, then `wanted_library` → the row shows `verdict == "wanted"`.
  - `test_tool_error_rolls_back`: `mark_same_track(x, x)` → a tool error, and no `DedupGroup` row exists.
  - `test_server_logs_never_contain_token`: start `airdrome mcp` as a subprocess with a test token and a free port, send one MCP `initialize` POST to the token path and one GET to `/mcp/wrong`, and stop it. Assert:
    - the token is not in the combined stdout+stderr;
    - the token path answered 200;
    - `/mcp/wrong` answered 404.
  - `test_mcp_refuses_without_token`: CliRunner with the token unset → exit code 1.
- [ ] **Step 2: Run** `uv run pytest tests/test_mcp_server.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests, then `ruff check . && ruff format --check . && uv run pytest`. Expected: all PASS, and `uvx ty check` shows no new diagnostics in the new files.
- [ ] **Step 5: Docs.**
  - **README:** a *MCP server* section covering:
    - generating the token (`python -c "import secrets;print(secrets.token_urlsafe(32))"` into `.env`);
    - running `airdrome mcp`;
    - registering with `claude mcp add --transport http --scope local airdrome http://127.0.0.1:8765/mcp/<token>`;
    - "never put the URL in `.mcp.json`".
  - **CLAUDE.md:**
    - the `mcp` command under *CLI surface*;
    - `mcpserver/` and `wanted/` under *Architecture*;
    - the per-call session as a second exception in *Session lifecycle*;
    - the two env vars in *Configuration*;
    - the reject-alias encoding under *Identity ladder*.
  - **ROADMAP:** mark the MCP item built and leave the Spotify importer next.
- [ ] **Step 6: Commit** `feat(mcp): airdrome mcp — FastMCP server with wanted, card and point edits`.

### Task 9: Live rollout (with the owner)

- [ ] **Step 1:** Ask the owner before recreating the live DB: the schema changed, so per *Migrations* it is dropped and the pipeline re-run (`import` → `land` ≈ 45 min → `organize` → `dedup`). Dedup groups come back from `duplicates.json`.
- [ ] **Step 2:** After the re-run, check `uv run airdrome status`. Expected: about 13.2k canonical tracks and about 3.2k groups from `wanted_library` (`total`).
- [ ] **Step 3:** Generate the token into `.env`, run `airdrome mcp` in the background, and register it with `claude mcp add --scope local`. Confirm `git status` shows no `.mcp.json` change.
- [ ] **Step 4:** Smoke test from a new Claude Code session: `library_status`, `wanted_scrobbled(limit=5)`, `track_card` on the first row.
- [ ] **Step 5:** Squash-merge `mcp-server` into `main` and push.
