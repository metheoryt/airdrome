"""Backend-agnostic playlist sync engine.

Drives one bidirectional sync pass between Airdrome and a single backend.
The 3-way merge operates on canonical `Track.id`s; backend-specific
translation is delegated to the `PlaylistAdapter`. Tracks unresolvable on
either side stay put on whichever side holds them — see `PlaylistLink`
docstring for the rule on what makes it into the snapshot.
"""

from collections import Counter
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


def _dedup(ids: list[int]) -> list[int]:
    """Drop duplicate IDs, preserving first-seen order.

    Every list the merge engine handles is conceptually a *set* of canonical
    track IDs — a playlist holds a track at most once. Dedup collapse can make
    several distinct source rows resolve to the same canon, so without this the
    duplicates compound on every sync (the bug that inflated synced playlists
    to ~100x their real size).
    """
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _three_way_merge(base: list[int], ours: list[int], theirs: list[int]) -> list[int]:
    """Merge two ordered ID lists against a common base.

    Removals from either side are respected. Additions from `theirs` that
    aren't already in `ours` get appended to the end. `ours` order is
    authoritative.
    """
    base_set = set(base)
    theirs_set = set(theirs)
    theirs_removed = base_set - theirs_set
    theirs_added = theirs_set - base_set

    merged = [t for t in ours if t not in theirs_removed]
    seen = set(merged)
    for t in theirs:
        if t in theirs_added and t not in seen:
            merged.append(t)
            seen.add(t)
    return merged


def _airdrome_canonical_ids(s: Session, playlist_id: int) -> list[int]:
    """Resolved canonical IDs of an Airdrome playlist, in order, *with* duplicates.

    Callers dedup for the merge; the raw (possibly duplicate-bearing) sequence is
    also what `changed_airdrome` is compared against so a bloated table gets
    rewritten clean on the next sync.
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
    ext: ExternalPlaylist,
    link: PlaylistLink | None,
    reset: bool = False,
) -> bool:
    """3-way merge one playlist with its backend mirror, applying deltas to both sides.

    The merge runs on deduplicated canonical-ID *sets*, and the backend write-back
    reconciles by backend ref identity (not canon membership) so it is idempotent
    even when `to_canonical_track`/`from_canonical_track` are not perfect inverses.
    `reset` rebuilds the backend from Airdrome's list, discarding the merge base and
    any backend-only additions (the force-resync escape hatch).

    Returns True if any change reached either side.
    """
    base = [] if reset else _dedup(link.synced_track_ids if link else [])
    raw_ours = _airdrome_canonical_ids(s, playlist.id)
    ours = _dedup(raw_ours)

    refs = adapter.get_track_refs(ext.id)
    ref_by_id: dict[str, ExternalTrackRef] = {r.id: r for r in refs}
    current_counts = Counter(r.id for r in refs)
    ref_to_canon: dict[str, int | None] = {rid: adapter.to_canonical_track(r) for rid, r in ref_by_id.items()}
    # On reset we ignore the backend's current contents for the merge so the result
    # is purely Airdrome's list; we still need `refs` above to clear stale rows.
    canon_to_ref: dict[int, ExternalTrackRef] = (
        {} if reset else {c: ref_by_id[rid] for rid, c in ref_to_canon.items() if c is not None}
    )
    theirs_canon = [] if reset else _dedup([c for c in ref_to_canon.values() if c is not None])

    merged = _three_way_merge(base, ours, theirs_canon)

    # Resolve the merged canon set to the backend refs it should be represented by.
    # Reuse the ref already in the backend when present (stable), else materialise one.
    desired_ids: list[str] = []
    desired_seen: set[str] = set()
    for canon in merged:
        ref = canon_to_ref.get(canon) or adapter.from_canonical_track(canon)
        if ref is None:
            # Airdrome has the track but the backend can't represent it — leave it
            # Airdrome-only; it stays out of the snapshot so it reads as steady state.
            continue
        if ref.id not in desired_seen:
            desired_seen.add(ref.id)
            ref_by_id.setdefault(ref.id, ref)
            desired_ids.append(ref.id)

    # Reconcile the backend to exactly `desired_ids` (one row each), keyed on ref id.
    added = removed = 0
    for rid in current_counts:
        if rid not in desired_seen:
            adapter.remove_track(ext.id, ref_by_id[rid])  # drops every row of this ref
            removed += 1
    for rid in desired_ids:
        count = current_counts.get(rid, 0)
        if count == 0:
            adapter.add_track(ext.id, ref_by_id[rid])
            added += 1
        elif count > 1:
            # Already present but duplicated — collapse to a single row.
            adapter.remove_track(ext.id, ref_by_id[rid])
            adapter.add_track(ext.id, ref_by_id[rid])
            removed += 1
            added += 1

    # Snapshot the canon set *as the backend now reports it*, so next run's `theirs`
    # matches `base` and an imperfect round-trip can't read as a one-sided delete.
    snapshot: list[int] = []
    for rid in desired_ids:
        canon = ref_to_canon[rid] if rid in ref_to_canon else adapter.to_canonical_track(ref_by_id[rid])
        if canon is not None:
            snapshot.append(canon)
    placed = _dedup(snapshot)

    changed_backend = added > 0 or removed > 0
    changed_airdrome = merged != raw_ours

    if changed_backend or not link or reset:
        adapter.commit()
    if changed_airdrome:
        _apply_to_airdrome(s, playlist.id, merged)

    now = datetime.now(UTC)
    if link is None:
        s.add(
            PlaylistLink(
                playlist_id=playlist.id,
                backend=adapter.backend,
                external_id=ext.id,
                synced_track_ids=placed,
                synced_at=now,
            )
        )
    else:
        link.synced_track_ids = placed
        link.external_id = ext.id  # heal in case backend rotated the id (rare)
        link.synced_at = now
    s.flush()

    return changed_backend or changed_airdrome


def sync(s: Session, adapter: PlaylistAdapter, reset: bool = False) -> None:
    """Run one bidirectional sync pass between Airdrome and a backend.

    When `reset` is set, every linked playlist is rebuilt from Airdrome's
    (deduplicated) canonical list instead of 3-way merged — the recovery lever
    for backend playlists that drifted or bloated. Backend-only playlists are
    still pulled in normally.
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
                # playlist; drop the stale link and let the next pass create
                # a fresh backend playlist.
                console.print(f"  [yellow]?[/yellow]  {playlist.name} (backend missing — relinking)")
                s.delete(link)
                s.flush()
                link = None

        if ext is None:
            ext = adapter.create(playlist)

        seen_external.add(ext.id)
        try:
            had_changes = _sync_pair(s, adapter, playlist, ext, link, reset=reset)
        except Exception:
            adapter.rollback()
            s.rollback()
            raise
        s.commit()  # backend already committed inside _sync_pair; durable per-playlist
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
            _sync_pair(s, adapter, playlist, ext, link=None)
        except Exception:
            adapter.rollback()
            s.rollback()
            raise
        s.commit()
        changed += 1
        console.print(f"  [cyan]<[/cyan]  {playlist.name}")
        total += 1

    console.print(f"[green]{changed}/{total} playlists updated[/green]")
