from sqlmodel import Session, func, select

from airdrome.console import console
from airdrome.models import Playlist, PlaylistTrack, Track

from ..models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


def _make_nv_playlist_track(track: Track, nv_playlist: NVPlaylist, nvs: Session) -> PlaylistTracks | None:
    if not track.main_file:
        return None

    media_file = nvs.exec(
        select(MediaFile).where(MediaFile.path == track.main_file.navidrome_path)
    ).one_or_none()
    if not media_file:
        console.print(f"[yellow]not found in Navidrome: {track.main_file.navidrome_path}[/yellow]")
        return None

    existing = nvs.exec(
        select(PlaylistTracks).where(
            PlaylistTracks.playlist_id == nv_playlist.id,
            PlaylistTracks.media_file_id == media_file.id,
        )
    ).one_or_none()
    if existing:
        return None

    pt_latest = nvs.exec(
        select(PlaylistTracks)
        .where(PlaylistTracks.playlist_id == nv_playlist.id)
        .order_by(PlaylistTracks.id.desc())
    ).first()
    next_id = (pt_latest.id + 1) if pt_latest else 1

    return PlaylistTracks(id=next_id, playlist_id=nv_playlist.id, media_file_id=media_file.id)


def _sync_playlist(
    playlist: Playlist, owner_id: int, s: Session, nvs: Session
) -> tuple[NVPlaylist, int, int]:
    nv_playlist = nvs.exec(select(NVPlaylist).where(NVPlaylist.name == playlist.name)).one_or_none()
    if not nv_playlist:
        nv_playlist = NVPlaylist(
            name=playlist.name,
            comment=playlist.description,
            owner_id=owner_id,
        )
        nvs.add(nv_playlist)
        nvs.flush()

    tracks_stmt = (
        select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id).order_by(PlaylistTrack.position)
    )

    added = total = 0
    for pt in s.exec(tracks_stmt):
        total += 1
        nv_pt = _make_nv_playlist_track(pt.track, nv_playlist, nvs)
        if nv_pt:
            added += 1
            nvs.add(nv_pt)
            if added % 100 == 0:
                nvs.flush()
    nvs.flush()

    count, total_duration, total_size = nvs.exec(
        select(
            func.count(),
            func.coalesce(func.sum(MediaFile.duration), 0),
            func.coalesce(func.sum(MediaFile.size), 0),
        )
        .join(PlaylistTracks)
        .where(PlaylistTracks.playlist_id == nv_playlist.id)
    ).one()

    nv_playlist.size = total_size
    nv_playlist.duration = total_duration
    nv_playlist.song_count = count
    nvs.flush()

    return nv_playlist, added, total


def sync_playlists_to_navi(s: Session, username: str):
    """
    For every canonical Playlist, get or create a Navidrome playlist and populate its tracks.
    Safe to rerun — existing entries are not duplicated.
    """
    with Session(get_nv_engine()) as nvs:
        user = nvs.exec(select(User).where(User.user_name == username)).one()

        playlists_handled = 0
        for playlist in s.exec(select(Playlist)):
            nv_playlist, added, total = _sync_playlist(playlist, user.id, s, nvs)
            if added > 0:
                playlists_handled += 1
            console.print(f"  [cyan]{added}/{total}[/cyan]  {nv_playlist.name}")

        nvs.commit()
        console.print(f"[green]{playlists_handled} playlists synced[/green]")
