# Manual dedup decisions as pairs

Status: built (2026-10-18). Full design brief and owner's decisions:
[2026-10-18-pair-decisions-design.md](../superpowers/specs/2026-10-18-pair-decisions-design.md).
Current-state map: `CLAUDE.md`, *Dedup decision mirror*.

## Why pairs

Manual dedup decisions used to be stored as groups (`DedupGroup` / `DedupGroupMember`): a group was
identified by the multiset of its members' `duplicate_hash`, each member carried the canon it was
assigned, and an all-NULL group meant "not duplicates". Groups do not compose. A page saved with a
changed membership left the old group beside the new one, and merging overlapping groups
last-writer-wins in row order made `dedup` drop a "same track" decision on one read order (groups
`w→v` and `x→w`). On the live DB, 2026-10-18: 22 clusters of overlapping groups; 13 merged cleanly,
5 named two canons for one group, 4 contradicted an older "not duplicates" with a newer "same". A single
point edit also needed five guards (a closure over affected groups, dead-member and unapplied-group
refusals, a collateral veto, a cross-recording-id refusal); four existed only because of the encoding.

A decision about a pair is one row: one edit is one row (plus at most one canon row), decisions never
overlap, and `dedup`'s output depends only on the decisions and the library, never on row order.

## Identity: natural key, not hash or id

A decision names a track by its natural key `(title, artist, album, album_artist)` — the raw columns of
the `Track` unique constraint, which is `NULLS NOT DISTINCT` so a key names at most one track.

- **Not `Track.id`:** ids do not survive a DB rebuild, and the DB is disposable; the mirror must restore.
- **Not `duplicate_hash`:** it includes mutable fields (track/disc number, year, a 5 s duration bucket),
  so tag backfill had to refuse to fill those fields on any track a decision named — the data model
  bent to keep a decision's key stable. With the natural key, backfill never touches a decision's key.

A decision whose end names no live track is *dead*: ignored, kept in the table, counted (`status`).
Tag edits that change the key kill the decision rather than silently re-pointing it.

## Newest wins

Pairs apply newest first (`decided_at`); a `same` joins two components unless an accepted newer
`different` separates them, a `different` separates unless a newer `same` joined them, and the loser is
superseded. Separations merge with components, so a cannot-link stays transitive. At an equal timestamp
every `same` is applied before any `different`, then by key order: the decisions migrated from one old
group share a time, and there `same` pairs were explicit choices while `different` pairs were only
implied by not being in the group. The order is total, so the result is independent of input order.
Upsert is newest-wins too: an older row never replaces a newer one on the same pair, which is what lets
a mirror import keep its timestamps. An MCP edit is by definition the newest decision.

## Why a manual `same` crosses recording ids

Automatic linking keeps one recording id per group. A manual `same` does not: the owner's "same" can
mean "fold this one into the one I listen to" (Drowning Pool's "Step Up (The Punisher Version)" into
Linkin Park's "Step Up", kept on purpose), not only "same recording". Manual beats automatic in both
directions: a `different` vetoes an automatic join, a `same` is never vetoed by recording evidence.

## The core rule (main file in a folded group)

In a manually folded group the main file must come from the canon's own recording, even when a folded-in
file has a higher bitrate. The canon's **core** is the canon plus the members automatic grouping alone
would join to it; the rest are *folded* (`Track.canon_folded`, written by the dedup apply step so
readers don't recompute it). `select_main`, `recompute_main_files`, the `organize` upgrade swap and
import `classify`'s "upgrade" comparison choose among the core's files; only a core with no file at all
falls back to the whole group, so the group still plays. Groups without a manual fold are unaffected.

## Edits equal `dedup`

An MCP edit stores its one row, replans the whole library with the CLI defaults (`plan_dedup`) and
applies the plan to the reachable tracks (`edit_scope`: a fixpoint over the plan's raw groups and the
current groups), so its result equals the following `dedup` run. This needs `dedup` to be idempotent;
the fingerprint representative of a track is therefore its highest-bitrate fingerprinted file and never
`is_main`, which the run itself rewrites.

## Rejected

- **An invariant patch on groups (closure plus guards).** Keep the group encoding and make every write
  re-encode the neighbours: take the closure of affected groups, refuse when a member is dead or the
  stored partition differs from the canon graph, veto collateral changes. That is the five-guard
  structure above, kept correct by construction work at each write; it also has false refusals (a
  one-hop-root check) and leaves the hash-key problem. Pairs make the invariants hold by representation.
- **A hash-keyed pair.** Pair decisions keyed by `duplicate_hash` fix the overlap but keep the mutable
  key: backfill must still refuse to fill hashed fields on named tracks, and a retag silently changes
  which tracks a decision means. The natural key is stable under every edit the pipeline makes itself.
