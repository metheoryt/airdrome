from .models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, engine as nv_engine
from sqlmodel import Session, select, func, or_, and_
from jellyfist.models import Track, engine, Playlist, TrackPlaylistLink


def sync_playlists(username: str):
    """
    For every playlist, get or create a Navidrome playlist.

    For every track in the playlist,
        search for a Navidrome media file and add it to the playlist, if not already added.

    The procedure is safe to rerun multiple times.
    """
    with Session(engine) as s, Session(nv_engine) as nvs:
        user = nvs.exec(select(User).where(User.user_name == username)).one()

        playlists_stmt = select(Playlist).where(
            Playlist.master == False, Playlist.music == False, Playlist.folder == False
        )

        playlists_handled = 0
        for playlist in s.exec(playlists_stmt):
            # search for the playlist in navidrome
            nv_playlist = nvs.exec(select(NVPlaylist).where(NVPlaylist.name == playlist.name)).one_or_none()
            if not nv_playlist:
                nv_playlist = NVPlaylist(
                    name=playlist.name,
                    comment=playlist.description,
                    owner_id=user.id,
                )
                nvs.add(nv_playlist)
                nvs.flush()
                # print("created navidrome playlist:", nv_playlist.name)

            playlist_tracks_stmt = (
                select(TrackPlaylistLink)
                .where(TrackPlaylistLink.playlist_id == playlist.id)
                .order_by(TrackPlaylistLink.added_at)
            )

            added = total = 0
            for tpl in s.exec(playlist_tracks_stmt):
                track = s.exec(select(Track).where(Track.id == tpl.track_id)).one()
                total += 1
                if not track.path:
                    # print("no file:", track.short_info)
                    continue

                media_file = nvs.exec(select(MediaFile).where(MediaFile.path == track.path)).one_or_none()
                if not media_file:
                    print("cant find in navidrome:", track.artist_album_name, track.path)
                    continue

                media_file: MediaFile
                mediafile_in_playlist = nvs.exec(
                    select(PlaylistTracks).where(
                        PlaylistTracks.playlist_id == nv_playlist.id,
                        PlaylistTracks.media_file_id == media_file.id,
                    )
                ).one_or_none()
                if mediafile_in_playlist:
                    # print("already in nv playlist:", track.short_info)
                    continue

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

                pt = PlaylistTracks(id=next_id, playlist_id=nv_playlist.id, media_file_id=media_file.id)
                nvs.add(pt)
                nvs.flush()
                added += 1

            stat = f"{added}/{total}"
            print(f"{stat:<9}", "tracks added to navidrome playlist", nv_playlist.name)
            if added > 0:
                playlists_handled += 1

        nvs.commit()
        print(playlists_handled, "playlists imported")
