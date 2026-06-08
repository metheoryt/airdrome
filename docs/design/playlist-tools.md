# Playlist editing tools — `merge` & `dedup-members`

**Status:** 🧭 designed 2026-06-09, not built. On-demand canonical-hub editing verbs that
complement the reconcile engine ([playlist-reconcile.md](playlist-reconcile.md)). Forward-looking
summary lives in [ROADMAP.md](../../ROADMAP.md) *Playlist management*.

## Mission — curate the hub, let `sync` carry it

Reconcile ([playlist-reconcile.md](playlist-reconcile.md)) keeps Airdrome's canonical playlists in
agreement with each remote, but it can't *clean up* the canonical side: it has no opinion on two
near-duplicate playlists that are really one list, or on a playlist that lists the same song twice.
Those are human-curation edits to the hub. This design adds two on-demand verbs that edit the
canonical `Playlist`/`PlaylistTrack` graph and nothing else; the existing `sync` then propagates the
result outward as ordinary "ours" changes. The verbs never talk to a remote.

## What the data actually showed (2026-06-09, live DB)

Scope was set on measured data, not impression:

- **Member duplication is real but modest.** Across 313 apple_ms playlists / 32,519 member rows,
  **91 playlists** hold duplicates — **803 redundant rows (~2.5%)**. Of the 787 duplicated
  canon-groups, **738 are canon-collapse** (different `track_id`s that `dedup` later linked to one
  canon) and only **52 rows** are literal repeated `track_id`s. → member-dedup must be
  **`canon_id`-aware**, and an apple_ms import-time dedup would catch almost none of it.
- **Exact-name playlist collisions are rare** (4 names, 8 playlists) and already handled by the old
  `land --merge-playlists`. The duplication that matters is **near-duplicate names** with distinct
  `source_id`s — `aggression 😈` / `aggressive 😈` / `aggressive 😈1`, `calm 🧘` / `calm ☯️` /
  `calm 🧘1`, `Alternative` / `Alternative1`, `Brakebeat` / `breakbeat`, `am keep` / `am keep 2`.
  These cannot be collapsed automatically with confidence → merge must be **human-directed**.

## The model rule that makes this safe

`SourcePlaylistRemote.to_canonical_track` already resolves a source track through `canon_id`
(`return track.canon_id or track.id`), and the orchestrator maps a canonical back to a source via
one `PlaylistLink.external_id` per `(playlist, provider)`. That yields the invariant the whole
design leans on:

> **A canonical playlist has at most one *live* source link per provider.**

Both merge cases fall out of it without touching the reconcile engine:

- **Cross-provider** same-name (apple_xml "X" + apple_ms "X") → one canonical, two provider links,
  each reconciles live. Already true today (playlist-reconcile decision #4).
- **Same-provider** absorption (two apple_ms lists — same-name *or* a near-dup `merge`) → the base's
  source link stays live; the others' tracks are folded in **once** and their identities are
  **tombstoned** (suppressed from recreation, not live remotes). Their future source-side edits
  don't flow — which is exactly the intent of "these are stale, fold them into the latest."

## CLI surface

A new top-level group `playlists` (siblings: `sync`, `navi`, `maint`):

```
airdrome playlists merge <base> <other>...      # fold others into base, tombstone them
airdrome playlists dedup-members [<name>...]     # collapse canon-duplicate rows; all playlists if none
airdrome playlists merge --same-name             # auto-group by normalized name, newest = base
```

Both verbs take `--dry-run`/`-n` and `-y`. A name argument resolves to exactly one playlist; a name
matching **>1** playlist prints the candidates with their ids and bails. `#<id>` is always accepted
to address a playlist unambiguously.

## The merge core — one function, three callers

`merge_playlists(s, base, others)` is the single mechanism:

1. Union `others`' members into `base`, **canon-resolved and deduped**, **appended at the end**
   (reconcile decision #2 — minimal diff, deterministic, idempotent, no reshuffle).
2. Write a **tombstone** row per absorbed playlist.
3. Delete the absorbed canonical `Playlist` (FK cascade drops its `PlaylistTrack` + `PlaylistLink`).

Three callers: explicit `playlists merge` (base = first arg), `playlists merge --same-name`
(base = newest `date_modified` per name group), and any future programmatic caller.

## Tombstone table — durability against re-`land`

When merge deletes absorbed playlist B, B's `SourcePlaylist` row still exists, so the next `land`'s
`get_or_create(platform, source_id)` would **recreate** B and silently undo the merge. The tombstone
prevents that:

```
playlist_merge:  provider, source_id  →  surviving_playlist_id     (PK: provider, source_id)
```

`unify_source_playlists` consults it and **skips** creating a canonical for any tombstoned
`(provider, source_id)`. A merge therefore sticks across every future import cycle.

## `dedup-members`

Collapse `PlaylistTrack` rows that resolve to the same canon — keep the earliest position, drop the
rest. Idempotent; no args = every playlist.

**Reconcile interaction (must be covered by a test):** dropping a redundant row is an "ours"
deletion relative to the `PlaylistLink` base. Because the base snapshot also carried the duplicate,
the multiset 3-way merge keeps it removed rather than resurrecting it on the next `sync`. This is
subtle enough to deserve an explicit test rather than trust.

## Decisions locked (2026-06-09)

1. **On-demand verbs, not a model invariant.** Duplication is ~2.5% of rows — a cleanup verb, not a
   reshaping of the just-built engine. *Rejected:* making canon-uniqueness a membership invariant
   enforced everywhere (seed/merge/dedup/sync); too much surgery near the new resolver for the
   payoff.
2. **Merge stays out of `land`.** `land` is purely mechanical per-source seeding that honors
   tombstones; it never merges. The `--merge-playlists` flag is **removed**. Same-name collapse
   moves to `playlists merge --same-name`, run as a sweep; tombstones keep it durable. Upholds
   reconcile decision #3 (land never reconciles / silently mutates curated playlists).
3. **Merge is human-directed; same-name is the only automatable grouping.** Near-dup names can't be
   grouped with confidence, so the explicit verb takes an exact base + others. `--same-name` is the
   one safe auto-grouping (exact normalized-name match, newest anchors).
4. **Tombstone = skip, not live re-route.** Absorbed same-provider identities are suppressed from
   recreation; their tracks fold in once. *Rejected:* re-routing an absorbed source's tracks into
   the surviving canonical on every land — it strains the one-live-link-per-provider rule and adds
   risk near the new sync engine for edits to playlists the user merged *because* they're stale.

## Follow-up — backend orphan cleanup (→ ROADMAP)

After a merge, an absorbed playlist's **Navidrome** counterpart becomes a stale orphan: its
`PlaylistLink` is gone, so `sync` neither updates nor deletes it. Deleting backend playlists that no
longer map to any canonical is its own concern (and its own risk surface) — parked to ROADMAP rather
than built here.
