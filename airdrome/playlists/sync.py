"""Backend-agnostic playlist sync engine.

Drives one bidirectional sync pass between Airdrome and a single backend.
The 3-way merge operates on canonical `Track.id`s; backend-specific
translation is delegated to the `PlaylistAdapter`. Tracks unresolvable on
either side stay put on whichever side holds them — see `PlaylistLink`
docstring for the rule on what makes it into the snapshot.
"""

from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from airdrome.console import console
from airdrome.enums import Platform
from airdrome.models import Playlist, PlaylistLink, PlaylistTrack, Track

from .adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter


def _resolve_canonical(s: Session, track_id: int, _depth: int = 0) -> int:
    """Follow `Track.canon_id` chains to the canonical track ID."""
    if _depth > 8:  # paranoia: shouldn't happen, but don't infinite-loop on a cycle
        return track_id
    track = s.get(Track, track_id)
    if track is None or track.canon_id is None or track.canon_id == track_id:
        return track_id
    return _resolve_canonical(s, track.canon_id, _depth + 1)


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
    rows = s.exec(
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id).order_by(PlaylistTrack.position)
    ).all()
    return [_resolve_canonical(s, pt.track_id) for pt in rows]


def _apply_to_airdrome(s: Session, playlist_id: int, merged_canon: list[int]) -> None:
    s.exec(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id))
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
) -> bool:
    """3-way merge one playlist with its backend mirror, applying deltas to both sides.

    Returns True if any change reached either side.
    """
    base = link.synced_track_ids if link else []
    ours = _airdrome_canonical_ids(s, playlist.id)

    refs = adapter.get_track_refs(ext.id)
    ref_to_canon: dict[ExternalTrackRef, int | None] = {r: adapter.to_canonical_track(r) for r in refs}
    theirs_canon: list[int] = [c for c in ref_to_canon.values() if c is not None]
    canon_to_ref: dict[int, ExternalTrackRef] = {c: r for r, c in ref_to_canon.items() if c is not None}

    merged = _three_way_merge(base, ours, theirs_canon)

    placed: list[int] = []
    theirs_set = set(theirs_canon)
    merged_set = set(merged)

    for canon in merged:
        if canon in theirs_set:
            placed.append(canon)
            continue
        ref = adapter.from_canonical_track(canon)
        if ref is None:
            # Airdrome has the track but the backend can't represent it.
            # Skip — it stays in Airdrome only and won't pollute the snapshot.
            continue
        adapter.add_track(ext.id, ref)
        placed.append(canon)

    for canon in theirs_set - merged_set:
        adapter.remove_track(ext.id, canon_to_ref[canon])

    changed_backend = len(placed) != len(theirs_canon) or set(placed) != theirs_set
    changed_airdrome = merged != ours

    if changed_backend or not link:
        adapter.commit()
    if changed_airdrome:
        _apply_to_airdrome(s, playlist.id, merged)

    now = datetime.now(timezone.utc)
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


def sync(s: Session, adapter: PlaylistAdapter) -> None:
    """Run one bidirectional sync pass between Airdrome and a backend."""

    airdrome_playlist_ids = [pid for pid in s.exec(select(Playlist.id).order_by(Playlist.name)).all()]
    seen_external: set[str] = set()
    changed = total = 0

    # 1. Existing Airdrome playlists — push or merge against the backend
    for playlist_id in airdrome_playlist_ids:
        playlist = s.get(Playlist, playlist_id)
        link = s.exec(
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
            had_changes = _sync_pair(s, adapter, playlist, ext, link)
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
            platform=Platform.NAVIDROME,  # generalise once a second backend lands
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
