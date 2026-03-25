from sqlmodel import Session, func, select

from airdrome.cloud.apple.models import ApplePlaylist, ApplePlaylistTrack, AppleTrack
from airdrome.console import console
from airdrome.models import Track, TrackFile, engine

from ..models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


def make_playlist_track(apt: ApplePlaylistTrack, nv_playlist: NVPlaylist, s: Session, nvs: Session):
    track: Track = apt.track.track
    if not track.main_file:
        return

    # match the track between the systems by the path
    media_file = nvs.exec(
        select(MediaFile).where(MediaFile.path == track.main_file.navidrome_path)
    ).one_or_none()
    if not media_file:
        console.print(f"[yellow]not found in Navidrome: {track.main_file.navidrome_path}[/yellow]")
        return

    media_file: MediaFile
    mediafile_in_playlist = nvs.exec(
        select(PlaylistTracks).where(
            PlaylistTracks.playlist_id == nv_playlist.id,
            PlaylistTracks.media_file_id == media_file.id,
        )
    ).one_or_none()
    if mediafile_in_playlist:
        return

    # get next id for the playlist
    pt_latest = nvs.exec(
        select(PlaylistTracks)
        .where(PlaylistTracks.playlist_id == nv_playlist.id)
        .order_by(PlaylistTracks.id.desc())
    ).first()
    if not pt_latest:
        next_id = 1
    else:
        next_id = pt_latest.id + 1

    return PlaylistTracks(id=next_id, playlist_id=nv_playlist.id, media_file_id=media_file.id)


def sync_apple_playlist(
    playlist: ApplePlaylist, owner_id: int, s: Session, nvs: Session
) -> tuple[NVPlaylist, int, int]:
    # search for the playlist in navidrome
    nv_playlist = nvs.exec(select(NVPlaylist).where(NVPlaylist.name == playlist.name)).one_or_none()
    if not nv_playlist:
        nv_playlist = NVPlaylist(
            name=playlist.name,
            comment=playlist.description,
            owner_id=owner_id,
        )
        nvs.add(nv_playlist)
        nvs.flush()
        # print("created navidrome playlist:", nv_playlist.name)

    playlist_tracks_stmt = (
        select(ApplePlaylistTrack)
        .join(AppleTrack)
        .join(Track)
        .join(TrackFile)
        .where(ApplePlaylistTrack.playlist_id == playlist.id, TrackFile.is_main.is_(True))
        .order_by(ApplePlaylistTrack.position)
    )

    added = total = 0
    for tpl in s.exec(playlist_tracks_stmt):
        total += 1
        pt = make_playlist_track(tpl, nv_playlist, s, nvs)
        if pt:
            added += 1
            nvs.add(pt)
            if added % 100 == 0:
                nvs.flush()
    nvs.flush()

    # sync playlist stats
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


def sync_apple_playlists_to_navi(username: str):
    """
    For every playlist, get or create a Navidrome playlist.

    For every track in the playlist,
        search for a Navidrome media file and add it to the playlist, if not already added.

    The procedure is safe to rerun multiple times.
    """
    with Session(engine) as s, Session(get_nv_engine()) as nvs:
        user = nvs.exec(select(User).where(User.user_name == username)).one()

        playlists_stmt = select(ApplePlaylist).where(
            ~ApplePlaylist.master, ~ApplePlaylist.music, ~ApplePlaylist.folder
        )

        playlists_handled = 0
        for playlist in s.exec(playlists_stmt):
            nv_playlist, added, total = sync_apple_playlist(playlist, user.id, s, nvs)
            if added > 0:
                playlists_handled += 1
            console.print(f"  [cyan]{added}/{total}[/cyan]  {nv_playlist.name}")

        nvs.commit()
        console.print(f"[green]{playlists_handled} playlists imported[/green]")
