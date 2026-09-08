"""Multi-remote playlist reconcile.

One pass that reconciles every canonical playlist against a set of remotes (cloud
sources and/or server backends), in the order given. For each playlist it gathers what
each remote currently holds versus its base and detects order-dependent (add-vs-remove)
conflicts across remotes. Clean playlists auto-merge via the pairwise engine in
`sync.py`; conflicted ones are settled non-interactively by `resolve_latest` — the last
remote that edited each conflicted track wins — and applied by forcing that membership
outward. `sync` never prompts.

Single-remote `sync <remote>` is just this with one adapter, where no cross-remote
conflict can arise. `sync all` passes sources first, then backends, so a source delete
reaches canonical before the backend push instead of being re-added.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import console, done
from airdrome.enums import Source
from airdrome.models import Playlist, PlaylistLink, Track

from .adapter import ExternalPlaylist, PlaylistAdapter
from .conflicts import PlaylistConflict, RemoteState, detect_conflicts, resolve_latest, verdicts
from .sync import _airdrome_canonical_ids, _apply_to_airdrome, _sync_pair, remote_membership


@dataclass
class _Ctx:
    """One remote's resolved state for a playlist, carried from pre-pass into apply."""

    adapter: PlaylistAdapter
    ext: ExternalPlaylist | None
    link: PlaylistLink | None
    theirs: list[int]
    base: list[int]


def _get_link(s: Session, playlist_id: int, remote: Source) -> PlaylistLink | None:
    return s.scalars(
        select(PlaylistLink).where(PlaylistLink.playlist_id == playlist_id, PlaylistLink.remote == remote)
    ).one_or_none()


def _gather(s: Session, playlist: Playlist, adapters: list[PlaylistAdapter]) -> list[_Ctx]:
    """Resolve each remote's link, current playlist, and membership for one playlist."""
    ctxs: list[_Ctx] = []
    for adapter in adapters:
        link = _get_link(s, playlist.id, adapter.remote)
        ext = adapter.get(link.external_id) if (link and link.external_id) else None
        if link is not None and ext is None and adapter.writable:
            # Backend playlist vanished — drop the stale link so the apply pass recreates it.
            console.print(f"  [yellow]?[/yellow]  {playlist.name} (backend missing — relinking)")
            s.delete(link)
            s.flush()
            link = None
        theirs = remote_membership(adapter, ext)
        base = link.synced_track_ids if link else []
        ctxs.append(_Ctx(adapter, ext, link, theirs, base))
    return ctxs


def _report_resolution(s: Session, conflict: PlaylistConflict) -> None:
    """Name each auto-resolved track and the remote that won it, so `sync` isn't silent.

    One line per conflicted track — conflicts are rare, and a bare `Track.id` would say
    nothing at the terminal, so the ids are resolved to titles here.
    """
    decided = verdicts(conflict)
    if not decided:
        return
    titles = {
        t.id: f"{t.title} — {t.artist}" if t.artist else t.title
        for t in s.scalars(select(Track).where(Track.id.in_(decided)))
    }
    for track_id, (remote, _) in decided.items():
        console.print(
            f"  [yellow]![/yellow]  {conflict.playlist_name}: "
            f"{titles.get(track_id, f'#{track_id}')} → {remote.value}"
        )


def _make_ext(adapter: PlaylistAdapter, playlist: Playlist) -> Callable[[], ExternalPlaylist]:
    return lambda: adapter.create(playlist)


def _apply_auto(s: Session, playlist: Playlist, ctxs: list[_Ctx]) -> bool:
    changed = False
    for ctx in ctxs:
        if _sync_pair(s, ctx.adapter, playlist, ctx.ext, ctx.link, _make_ext(ctx.adapter, playlist)):
            changed = True
    return changed


def _apply_override(s: Session, playlist: Playlist, ctxs: list[_Ctx], final: list[int]) -> bool:
    """Force `final` as the playlist's membership and propagate it to every remote.

    Setting each remote's base to its current `theirs` makes the pairwise merge collapse
    to `final` (ours + theirs - theirs), so the writable remotes are pushed to it and the
    read-only ones simply re-base — without a separate write-back code path.
    """
    _apply_to_airdrome(s, playlist.id, final)
    for ctx in ctxs:
        if ctx.link is not None:
            ctx.link.synced_track_ids = list(ctx.theirs)
            s.flush()
        _sync_pair(s, ctx.adapter, playlist, ctx.ext, ctx.link, _make_ext(ctx.adapter, playlist))
    return True


def _pull_backend_only(s: Session, adapter: PlaylistAdapter, seen: set[str]) -> int:
    """Create canonical playlists for backend playlists Airdrome doesn't mirror yet."""
    pulled = 0
    for ext in adapter.list_playlists():
        if ext.id in seen:
            continue
        playlist = Playlist(name=ext.name, platform=adapter.remote, source_id=ext.id, description=ext.comment)
        s.add(playlist)
        s.flush()
        try:
            _sync_pair(s, adapter, playlist, ext, None, lambda e=ext: e)
        except Exception:
            adapter.rollback()
            s.rollback()
            raise
        s.commit()
        console.print(f"  [cyan]<[/cyan]  {playlist.name}")
        pulled += 1
    return pulled


def reconcile(s: Session, adapters: list[PlaylistAdapter]) -> None:
    """Reconcile every canonical playlist against `adapters`, in order.

    Single pass, no prompting: a playlist with no hard conflict auto-merges pairwise; a
    conflicted one is settled by `resolve_latest` and that membership forced outward.
    """
    playlist_ids = list(s.scalars(select(Playlist.id).order_by(Playlist.name)).all())

    changed = total = 0
    seen: dict = {a.remote: set() for a in adapters}
    for pid in playlist_ids:
        playlist = s.get(Playlist, pid)
        if playlist is None:  # deleted between the id scan and now
            continue
        ctxs = _gather(s, playlist, adapters)
        states = [RemoteState(c.adapter.remote, list(c.base), list(c.theirs)) for c in ctxs]
        conflicts = detect_conflicts(states)
        try:
            if conflicts:
                conflict = PlaylistConflict(
                    playlist_id=playlist.id,
                    playlist_name=playlist.name,
                    ours=_airdrome_canonical_ids(s, playlist.id),
                    states=states,
                    conflicts=conflicts,
                )
                had = _apply_override(s, playlist, ctxs, resolve_latest(conflict))
                _report_resolution(s, conflict)
            else:
                had = _apply_auto(s, playlist, ctxs)
        except Exception:
            for ctx in ctxs:
                ctx.adapter.rollback()
            s.rollback()
            raise
        s.commit()
        for ctx in ctxs:
            link = _get_link(s, playlist.id, ctx.adapter.remote)
            if link and link.external_id:
                seen[ctx.adapter.remote].add(link.external_id)
        changed += int(had)
        total += 1
        console.print(f"  {'[green]+[/green]' if had else '[dim]=[/dim]'}  {playlist.name}")

    # Backend-only playlists are pulled in; sources don't create canonical playlists (land does).
    for adapter in adapters:
        if adapter.writable:
            pulled = _pull_backend_only(s, adapter, seen[adapter.remote])
            changed += pulled
            total += pulled

    done(f"{changed}/{total} playlists reconciled")
