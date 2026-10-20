# Spec inventory (2026-10-20)

Input for a future `SPEC.md`: every hard rule airdrome already promises (taken from CLAUDE.md and
README), with the tests that hold it. Calibration (thresholds, flag sets, trigram weights) is left out
on purpose: it is measured on data, not specified.

Status: **covered** — a test fails if the rule breaks; **partial** — tested for some paths or only
indirectly; **none** — no test. Area prefixes are the draft `REQ-*` families.

## Totals

| Status | Count |
|---|---|
| covered | 53 |
| partial | 5 |
| none | 7 |

## Defects the gap tests found (2026-10-20)

- **Fixed:** `sync -n` committed anyway — `reconcile` commits once per playlist — and `sync navidrome -n`
  wrote Navidrome's DB for real. `reconcile` and `NavidromeAdapter` now take `dry_run`.
- **Fixed:** `dedup` had no `--dry-run` although the CLI help promises it on every write command.
- **Fixed (2026-10-20):** a read-only source playlist never reconciled. `_gather` looked a source up
  only through its `PlaylistLink`, and nothing creates one for a source (`land` seeds the playlist but no
  link), so `sync apple_xml` / `sync apple_ms` were no-ops since they were built (317 playlists, 0 links).
  The first sync now finds the source by the playlist's own `platform`/`source_id`. Every earlier test
  seeded the link by hand; `test_first_source_sync_links_the_playlist_it_was_landed_from` does not.
- **Dead code:** `Base.truncate_cascade` (commits on its own) has no caller.

## Gaps (none / partial), worst first

| Rule | Status | Why it matters |
|---|---|---|
| IMPORT-1 every path is resolved before anything is written; one unrecognized path writes nothing | partial | only `detect` is tested, not "nothing written" |
| IMPORT-2 Spotify plays under 30 s are not imported | none | no Spotify, Last.fm or ListenBrainz parser test at all (only LB ids) |
| LAND-1 `augment_aliases` and `copy_plays` (alias plays become `TrackPlay` history) | none | play counts pushed to Navidrome come from here |
| ORG-1 any re-bind other than the upgrade swap relocates no file | partial | |
| ORG-2 two tracks never plan the same destination | none | known gap, not handled in code either (CLAUDE.md, *organize*) |
| DEDUP-1 `loved` takes no part in canon ordering | none | |
| LADDER-1 every binding records method and confidence | partial | checked per rung, not as a rule |
| MCP-1 edit and verdict tools hold one write lock | none | overlapping commits could write mirror snapshots out of order |
| SEC-1 every Typer app sets `pretty_exceptions_show_locals=False` | none | a traceback would print the AcoustID key or MCP token |
| CLI-2 `status` never migrates | partial | unreachable DB is tested, not "no migration" |
| MAINT-1 `maint renormalize` recomputes every `_norm` field | none | |
| LADDER-2 Track rows are never re-keyed or merged | partial | `test_tags_landing_in_foreign_group_is_not_rekeyed` only |
| PLAY-1 importing a playlist snapshot again never re-unions an existing canonical playlist | covered by one test | listed here because it is the only guard of a user-visible rule |

## Covered

| Rule | Tests |
|---|---|
| SEC-2 AcoustID key never appears in errors or output | `test_acoustid.py::test_invalid_key_abort_message_has_no_key`, `test_lookup_failures_are_key_free_errors` |
| SEC-3 MCP token never in server logs | `test_mcp_server.py::test_server_logs_never_contain_token` |
| CLI-3 `organize` defaults to copy | `test_cli_defaults.py::test_organize_defaults_to_copy` |
| CLI-4 `dedup` with no `--set` uses the recommended sets | `test_dedup_defaults_to_recommended_sets` |
| CLI-5 `status` reports an unreachable DB instead of crashing | `test_status_reports_unreachable_db` |
| IMPORT-3 music folders: only new recordings and upgrades are imported | `test_import_classify.py` (repeats, have, upgrade, new) |
| IMPORT-4 a re-run reuses the scan cache and re-fingerprints nothing | `test_rerun_uses_cache_and_adds_nothing` |
| IMPORT-5 Apple path rebuild beats the file's tags | `test_pathindex.py::test_path_beats_matching_tags` |
| LADDER-3 nothing binds or links across two recording ids | `test_ladder.py` (foreign id, conflicted track), `test_dedup_recording.py::test_fingerprint_never_joins_two_recording_ids` |
| LADDER-4 a fingerprint match links only within 2 s of duration | `test_fp_within_two_seconds_links`, `test_fp_duration_gap_not_linked` |
| LADDER-5 fingerprints compare as signed int32 | `test_fingerprint.py::test_values_above_int31_equal_signed_form` |
| LADDER-6 the ListenBrainz-mapped id never binds, only breaks ties | `test_match_aliases.py::test_mapped_id_*` |
| LADDER-7 a rejected alias stays unbound, loses its plays, survives `land` | `test_reject_removes_plays_and_survives_land`, `test_rejected_alias_is_not_rematched` |
| LADDER-8 a track's recording id is the one its files agree on | `test_recording_of_track_*` |
| DEDUP-2 `canon_id` is terminal (no chains) | `test_dedup_persistence.py`, `test_mbid_existing_canon_never_chained` |
| DEDUP-3 a manual `different` is a transitive cannot-link unless a newer `same` joined them | `test_dedup_decisions.py`, `test_dedup_grouping.py::test_cannot_link_*` |
| DEDUP-4 a manual `same` crosses recording ids | `test_manual_same_links_across_recording_ids` |
| DEDUP-5 decision resolution does not depend on row order | `test_repro_both_read_orders_agree`, `test_ties_resolve_identically_in_both_input_orders` |
| DEDUP-6 dead decisions are ignored, kept and counted | `test_dead_decision_ignored_counted_and_kept` |
| DEDUP-7 `dedup` is idempotent | `test_auto_idempotent_on_repeat`, `test_dedup_twice_gives_the_same_canons` |
| DEDUP-8 the newest canon choice wins over `canon_order` | `test_canon_choice_wins_over_canon_order`, `test_newest_canon_choice_wins_in_merged_group` |
| DEDUP-9 the compilation rank leads canon order | `test_compilation_canon_when_two_or_more` |
| DEDUP-10 a track whose selected non-title fields are all blank is skipped | `test_compute_groups_skips_blank_active_key_field` |
| DEDUP-11 a point edit gives the same result as the following `dedup` | `test_edit_equals_full_dedup`, `test_edit_on_undeduped_db_matches_full_dedup` |
| DEDUP-12 a point edit re-picks the main only for the touched groups | `test_edit_leaves_unrelated_groups_main_alone` |
| DEDUP-13 a point edit is the newest decision even against future-dated rows | `test_edit_beats_*_future_dated_pair` |
| MIRROR-1 the mirror is written after commit, never on rollback | `test_mirror_written_after_commit`, `test_rollback_drops_the_snapshot` |
| MIRROR-2 an empty export writes an empty v2 document | `test_clearing_every_decision_writes_empty_v2` |
| MIRROR-3 restore seeds only empty tables | `test_restore_seeds_empty_tables`, `test_restore_leaves_non_empty_tables_untouched` |
| MIRROR-4 a legacy or unparsable mirror is refused, left untouched | `test_restore_refuses_legacy_file_and_leaves_it_untouched`, `test_restore_raises_on_an_unparsable_mirror` |
| MIRROR-5 import: newest wins row by row, duplicates rejected, future clamped | `test_dedup_mirror.py::test_import_*` |
| MIRROR-6 `dedup-import` refuses the live mirror in the legacy format | `test_dedup_import_refuses_the_live_legacy_mirror`, `test_script_refuses_live_mirror_path` |
| MAIN-1 exactly one main file per group | `test_recompute_mains.py::test_recompute_collapses_two_mains_after_merge`, `test_different_keeps_one_main_in_both_halves` |
| MAIN-2 the main comes from the core of a manually folded group | `test_main_stays_with_core_when_folded_file_is_better`, `test_main_falls_back_when_core_has_no_file` |
| MAIN-3 best copy = bitrate, then container | `test_select_main_*` |
| ORG-3 the upgrade swap never overwrites; a failed swap puts the old main back | `test_organize.py::test_failed_swap_*`, `test_failed_cross_extension_swap_removes_the_partial_upgrade` |
| ORG-4 `--dry-run` touches no file and plans the real path | `test_dry_run_touches_no_files_but_still_reports`, `test_dry_run_plans_the_same_path_a_real_run_would_write` |
| ORG-5 a folded-in better file is not swapped in | `test_organize_does_not_swap_in_folded_upgrade` |
| NAVI-1 `navi push` and `sync navidrome`/`all` refuse while Navidrome listens, even with `-y`; without `-y` a no aborts | `test_navi_guard.py::test_refuses_while_navidrome_runs`, `test_push_asks_and_a_no_writes_nothing` |
| NAVI-2 the WAL is checkpointed (truncated) before writing Navidrome's DB | `test_push_checkpoints_before_writing`, `test_checkpoint_wal_empties_the_wal` |
| CLI-1 `--dry-run` writes nothing on every write command, Navidrome's DB included | `test_cli_dry_run.py` (each command with and without `-n`) |
| NAVI-3 plays are summed and rating/loved aggregated across the group | `test_navidrome_sync.py` |
| PLAY-2 a read-only remote is never written and stores `theirs` as base | `test_readonly_remote_pulls_without_writing`, `test_resurrection_bug_is_dead` |
| PLAY-3 a conflict goes to the last remote that edited the track; an untouched remote abstains | `test_playlist_conflicts.py` |
| PLAY-4 the Navidrome guard fires only with a writable backend in scope | `test_sync_source_reconciles_one_readonly_remote_without_guard` |
| MCP-2 `mcp` refuses without a token and off head, never migrates | `test_mcp_refuses_without_token`, `test_mcp_refuses_off_head_without_migrating` |
| MCP-3 an edit on rebuilt, empty tables restores the mirror first | `test_edit_after_rebuild_keeps_mirrored_decisions`, `test_rebuild_while_server_runs_keeps_mirrored_decisions` |
| MCP-4 one tool call is one transaction | `test_tool_error_rolls_back`, `test_tool_session_rolls_back_flushed_work_on_error` |
| MCP-5 only `ToolError` messages reach the client | `test_unexpected_errors_are_masked` |
| WANT-1 `skip` beats `wanted`; a verdict on a twin is stored on the canon | `test_wanted_verdicts.py` |
| SEARCH-1 whole words, never substrings (`a-ha` ≠ Baha Men) | `test_search.py::test_matches` |
