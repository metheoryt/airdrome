"""Backend-agnostic playlist sync engine.

Drives one bidirectional sync pass between Airdrome and a single backend.
The 3-way merge operates on canonical `Track.id`s; backend-specific
translation is delegated to the `PlaylistAdapter`. Tracks unresolvable on
either side stay put on whichever side holds them — see `PlaylistLink`
docstring for the rule on what makes it into the snapshot.
"""

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.enums import Source
from airdrome.models import Playlist, PlaylistLink, PlaylistTrack, Track

from .adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter


def _resolve_canonical(s: Session, track_id: int) -> int:
    """Resolve to the canonical track ID with a single hop.

    canon_id is terminal by invariant (see Track.canon_id; enforced by
    flatten_canon_chains), so no chain walking is needed.
    """
    track = s.get(Track, track_id)
    if track is None or track.canon_id is None or track.canon_id == track_id:
        return track_id
    return track.canon_id


def _three_way_merge(base: list[int], ours: list[int], theirs: list[int]) -> list[int]:
    """Multiset 3-way merge that preserves duplicate entries.

    A playlist may list the same canonical track more than once, so this works
    on *counts*, not sets. With a known `base` the merged count of a track is
    ``ours + theirs - base`` clamped at zero, so additions and removals from
    either side both apply. With no base (the first sync of a pair) we cannot
    tell a genuine add from shared history, so we union by the larger
    multiplicity instead. `ours` order leads; net backend-side additions are
    appended in `theirs` order.
    """
    base_c, ours_c, theirs_c = Counter(base), Counter(ours), Counter(theirs)
    has_base = bool(base)
    target: dict[int, int] = {}
    for cid in set(base_c) | set(ours_c) | set(theirs_c):
        if has_base:
            target[cid] = max(0, ours_c[cid] + theirs_c[cid] - base_c[cid])
        else:
            target[cid] = max(ours_c[cid], theirs_c[cid])

    merged: list[int] = []
    emitted: Counter[int] = Counter()
    for cid in ours:  # our order and multiplicity first
        if emitted[cid] < target[cid]:
            merged.append(cid)
            emitted[cid] += 1
    for cid in theirs:  # then whatever extra copies the backend contributes
        if emitted[cid] < target[cid]:
            merged.append(cid)
            emitted[cid] += 1
    return merged


def _airdrome_canonical_ids(s: Session, playlist_id: int) -> list[int]:
    """Resolved canonical IDs of an Airdrome playlist, in order, *with* duplicates.

    Multiplicity is meaningful — the playlist may hold a track more than once —
    so the duplicates are kept; the merge mirrors them faithfully.
    """
    rows = s.scalars(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id).order_by(PlaylistTrack.position)
    ).all()
    return [_resolve_canonical(s, pt.track_id) for pt in rows]


def _apply_to_airdrome(s: Session, playlist_id: int, merged_canon: list[int]) -> None:
    s.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id))
    s.flush()
    for pos, track_id in enumerate(merged_canon, start=1):
        s.add(PlaylistTrack(playlist_id=playlist_id, track_id=track_id, position=pos))
    s.flush()


def _sync_pair(
    s: Session,
    adapter: PlaylistAdapter,
    playlist: Playlist,
    ext: ExternalPlaylist | None,
    link: PlaylistLink | None,
    make_ext: Callable[[], ExternalPlaylist],
) -> bool:
    """Multiset 3-way merge one playlist with its backend mirror, both directions.

    Duplicate entries are preserved: the merge mirrors multiplicity, and the
    backend write-back reconciles *counts* keyed on ref identity (not canon
    membership) so it stays idempotent even when `to_canonical_track` and
    `from_canonical_track` are not perfect inverses. `ext` is None when no backend
    playlist exists yet; `make_ext` lazily creates one only once there is at least
    one track to push, so empty playlists never reach the backend.

    Returns True if any change reached either side.
    """
    raw_ours = _airdrome_canonical_ids(s, playlist.id)
    base = link.synced_track_ids if link else []
    ours = raw_ours

    refs = adapter.get_track_refs(ext.id) if ext is not None else []
    ref_by_id: dict[str, ExternalTrackRef] = {r.id: r for r in refs}
    current_counts = Counter(r.id for r in refs)
    ref_to_canon: dict[str, int | None] = {rid: adapter.to_canonical_track(r) for rid, r in ref_by_id.items()}
    theirs = [c for r in refs if (c := ref_to_canon[r.id]) is not None]  # ordered, with multiplicity
    canon_to_ref: dict[int, ExternalTrackRef] = {
        c: ref_by_id[rid] for rid, c in ref_to_canon.items() if c is not None
    }

    merged = _three_way_merge(base, ours, theirs)
    changed_airdrome = merged != raw_ours

    # Resolve each merged copy to a backend ref, preserving multiplicity. Reuse the
    # ref already in the backend when present (stable), else materialise one.
    desired: list[str] = []
    for canon in merged:
        ref = canon_to_ref.get(canon) or adapter.from_canonical_track(canon)
        if ref is None:
            # Airdrome has the track but the backend can't represent it — leave it
            # Airdrome-only; it stays out of the snapshot so it reads as steady state.
            continue
        ref_by_id.setdefault(ref.id, ref)
        desired.append(ref.id)
    desired_counts = Counter(desired)

    # Lazily create the backend playlist only if there is something to mirror.
    if ext is None and desired:
        ext = make_ext()
        current_counts = Counter()  # brand-new playlist starts empty

    # Reconcile per-ref counts. `remove_track` clears every row of a ref, so a ref
    # whose count changed is wiped and re-added at the wanted multiplicity. Process
    # wanted refs in `desired` (merged) order first, then any refs only the backend
    # still holds, so additions land deterministically rather than in set order.
    added = removed = 0
    if ext is not None:
        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        for rid in desired + list(current_counts):
            if rid not in seen_ids:
                seen_ids.add(rid)
                ordered_ids.append(rid)
        for rid in ordered_ids:
            have, want = current_counts.get(rid, 0), desired_counts.get(rid, 0)
            if have == want:
                continue
            if have:
                adapter.remove_track(ext.id, ref_by_id[rid])
                removed += have
            for _ in range(want):
                adapter.add_track(ext.id, ref_by_id[rid])
                added += 1

    # Snapshot the canon multiset *as the backend now reports it*, so next run's
    # `theirs` matches `base` and an imperfect round-trip can't read as a delete.
    snapshot: list[int] = []
    for rid in desired:
        canon = ref_to_canon[rid] if rid in ref_to_canon else adapter.to_canonical_track(ref_by_id[rid])
        if canon is not None:
            snapshot.append(canon)

    changed_backend = added > 0 or removed > 0

    if changed_backend:
        adapter.commit()
    if changed_airdrome:
        _apply_to_airdrome(s, playlist.id, merged)

    if ext is not None:
        now = datetime.now(UTC)
        if link is None:
            s.add(
                PlaylistLink(
                    playlist_id=playlist.id,
                    backend=adapter.backend,
                    external_id=ext.id,
                    synced_track_ids=snapshot,
                    synced_at=now,
                )
            )
        else:
            link.synced_track_ids = snapshot
            link.external_id = ext.id  # heal in case backend rotated the id (rare)
            link.synced_at = now
        s.flush()

    return changed_backend or changed_airdrome


def sync(s: Session, adapter: PlaylistAdapter) -> None:
    """Run one bidirectional sync pass between Airdrome and a backend.

    Duplicate entries are mirrored faithfully (multiplicity is preserved on both
    sides). A playlist with no backend-representable tracks creates no backend
    playlist. Backend-only playlists are pulled into Airdrome.
    """

    airdrome_playlist_ids = list(s.scalars(select(Playlist.id).order_by(Playlist.name)).all())
    seen_external: set[str] = set()
    changed = total = 0

    # 1. Existing Airdrome playlists — push or merge against the backend
    for playlist_id in airdrome_playlist_ids:
        playlist = s.get(Playlist, playlist_id)
        link = s.scalars(
            select(PlaylistLink).where(
                PlaylistLink.playlist_id == playlist.id,
                PlaylistLink.backend == adapter.backend,
            )
        ).one_or_none()
        ext: ExternalPlaylist | None = None

        if link is not None:
            ext = adapter.get(link.external_id)
            if ext is None:
                # Backend playlist deleted out from under us. Keep the Airdrome
                # playlist; drop the stale link and let this pass create a fresh
                # backend playlist (lazily, only if it has tracks to mirror).
                console.print(f"  [yellow]?[/yellow]  {playlist.name} (backend missing — relinking)")
                s.delete(link)
                s.flush()
                link = None

        try:
            # `create` is deferred into _sync_pair so a playlist with nothing to
            # mirror never spawns an empty backend playlist.
            had_changes = _sync_pair(s, adapter, playlist, ext, link, lambda p=playlist: adapter.create(p))
        except Exception:
            adapter.rollback()
            s.rollback()
            raise
        s.commit()  # backend already committed inside _sync_pair; durable per-playlist

        # The link (if any) now points at whatever backend playlist we settled on.
        synced_link = s.scalars(
            select(PlaylistLink).where(
                PlaylistLink.playlist_id == playlist.id,
                PlaylistLink.backend == adapter.backend,
            )
        ).one_or_none()
        if synced_link is not None:
            seen_external.add(synced_link.external_id)

        if had_changes:
            changed += 1
            console.print(f"  [green]+[/green]  {playlist.name}")
        else:
            console.print(f"  [dim]=[/dim]  {playlist.name}")
        total += 1

    # 2. Backend-only playlists — pull into Airdrome
    for ext in adapter.list_playlists():
        if ext.id in seen_external:
            continue
        playlist = Playlist(
            name=ext.name,
            platform=Source.NAVIDROME,  # generalise once a second backend lands
            source_id=ext.id,
            description=ext.comment,
        )
        s.add(playlist)
        s.flush()
        try:
            # ext already exists here, so make_ext is never invoked (bind it anyway).
            _sync_pair(s, adapter, playlist, ext, link=None, make_ext=lambda e=ext: e)
        except Exception:
            adapter.rollback()
            s.rollback()
            raise
        s.commit()
        changed += 1
        console.print(f"  [cyan]<[/cyan]  {playlist.name}")
        total += 1

    console.print(f"[green]{changed}/{total} playlists updated[/green]")
