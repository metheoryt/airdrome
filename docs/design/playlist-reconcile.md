# Playlist reconcile — Airdrome as hub

**Status:** ✅ built 2026-06-08 as `airdrome sync <remote>` / `sync all`. "How it works today"
lives in [AGENTS.md](../../AGENTS.md) *Playlist reconcile*; this file is the long-form rationale
and the alternatives we rejected.

## Mission — what is the source of truth?

The trigger for this design was "what is the SoT for playlists?" Neither a source nor a backend
can be it:

- **A source can't be SoT** — you can't write back to an Apple/Spotify export (no API). It can only
  *propose*.
- **A backend can't be SoT** — if Navidrome is SoT, a *second* backend (future Plex) has no authority.

Only **Airdrome-as-hub** lets every peer be a symmetric *remote*, and makes a new destination "just
another remote."

## Git framing (the spine)

- **Airdrome = the repo / working tree** (truth).
- **Sources (Apple, Spotify) = read-only remotes** — Airdrome `fetch`es changes from them; never
  writes back. They only propose.
- **Backends (Navidrome, future Plex) = read-write remotes** — fetch (pull) and push.
- **reconcile = merge**; **conflicts = merge conflicts** resolved interactively per playlist.

## The one mechanism — reconcile-with-base, per remote

Before this, there were two disconnected boundaries:

- **Boundary A — source → canonical** (`unify_source_playlists`): a blind **append-only union**,
  no base.
- **Boundary B — canonical ↔ backend** (`playlists/sync.py`): a proper multiset 3-way merge with a
  base (`PlaylistLink.synced_track_ids`).

Unify both under one operation, per `(playlist, remote)`:

```
base   = canonical membership at last reconcile with THIS remote
theirs = remote's current membership, mapped to canonical Track ids
ours   = current canonical
→ multiset 3-way merge (existing _three_way_merge); update base afterward
```

A source export is a *complete* statement of the playlist at export time, so `theirs` is fully known
and `base→theirs` is exactly "what the source changed since last import."

## This kills the re-import resurrection bug

Concrete failure before the fix: push v1 → delete track X in Navidrome → pull (X removed from
canonical) → import a newer source snapshot that still lists X → boundary A's blind union re-adds X
→ next push sends X back to Navidrome. **The downstream deletion was silently undone.**

Giving the *source* a base makes `base→theirs` show "source didn't change X" while `base→ours` shows
"we deleted X" → merge keeps it deleted. **The bug dies from the model, not a patch.**

## Interactive resolution (modeled on `dedup`)

- Default = auto multiset 3-way, silent for clean merges, with a per-playlist change summary.
- A **hard conflict** = the same playlist got *contradictory* edits from >1 remote since each last
  reconciled (e.g. Apple still lists X, Navidrome deleted X, each vs its own base). This is the
  **order-dependent** case: a track one remote added and another removed. Pure adds from different
  remotes union and never conflict, so conflicts are rare.
- Hard conflicts force those playlists into the resolver (can't silently guess). `--review` opens it
  for *every* changed playlist.
- Strategies offered **per playlist** (not per track, to keep the decision surface small): take a
  remote (overwrite) / keep ours / auto 3-way / abort. No per-track manual editor.

## Decisions locked (2026-06-08)

1. Source `source_id` is **stable across snapshots** → the per-remote base keys off the remote's own
   identity; no name-based fallback heuristics.
2. **Drop ordering as a semantic, but no gratuitous reshuffle.** The merge is a minimal diff against
   the canonical's current order: removes delete those `PlaylistTrack` rows in place, adds append at
   the end, everything else stays put. Deterministic + idempotent — a no-change reconcile touches
   nothing.
3. **Reconcile is its own command/step, NOT folded into `land`.** `land` stays mechanical (build
   canonical from sources, no playlist reconciliation). Importing a snapshot must never silently
   mutate curated playlists — you opt into reconcile.
4. **apple_xml vs apple_ms are separate remotes** — each gets its own base; the same playlist can
   appear in both Apple exports. Under `merge_playlists`, one canonical carries two source links that
   merge independently. *Rejected:* deduping to one "apple" remote.

## Rejected alternatives

- **Source or backend as SoT** — rejected (see Mission): neither can hold authority over a second peer.
- **Replace the 3-way merge with explicit push/pull** (an earlier ROADMAP idea) — rejected. The
  conclusion was the opposite: the merge is the right primitive; the fix was to apply it at *both*
  boundaries and add a human seat. push/pull survive as *directional shortcuts* over the one engine
  (push = favor ours→backend; pull = favor backend→ours), not a separate code path.
- **Per-track resolver UI** — rejected as too fine a decision surface; resolution is per playlist.

## Deviation from the original plan (as built)

`land` does **not** create source `PlaylistLink`s. The source base is seeded by the first `sync`
(empty base → union), which fixes the resurrection bug just as well and is simpler. So
`unify_source_playlists` only writes membership when it *creates* a canonical playlist; existing
playlists are owned by `sync`.

## 🅿️ Parked — extend the hub/remotes/base model to tracks

Track metadata, ratings, loved, and play history reconciled per-remote against a base. Genuinely the
same engine; richer conflict surface than playlists (which *field* wins, not just membership in/out).
Play counts already flow one-way via `navi push` stats; the general version makes that bidirectional
and base-aware. Its own discussion before building.
