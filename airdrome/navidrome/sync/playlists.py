from sqlmodel import Session, delete, func, select

from airdrome.console import console
from airdrome.models import Playlist, PlaylistTrack, Track

from ..models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


class NVPlaylistSyncer:
    def __init__(self, s: Session, username: str):
        self.s = s
        self.username = username
        self._user: User | None = None

    def drop_navi_playlists(self):
        with Session(get_nv_engine()) as nvs:
            user = self._get_user(nvs)
            nvs.exec(delete(NVPlaylist).where(NVPlaylist.owner_id == user.id))
            nvs.commit()

    def push_navi_playlists(self):
        """
        For every canonical Playlist, get or create a Navidrome playlist and populate its tracks.
        Safe to rerun — existing entries are not duplicated.
        """
        with Session(get_nv_engine()) as nvs:
            playlists_handled = 0
            for playlist in self.s.exec(select(Playlist).order_by(Playlist.name)):
                nv_playlist, added, total = self._sync_playlist(playlist, nvs)
                if added > 0:
                    playlists_handled += 1
                console.print(f"  [cyan]{added}/{total}[/cyan]  {nv_playlist.name}")

            nvs.commit()
            console.print(f"[green]{playlists_handled} playlists synced[/green]")

    def _get_user(self, nvs: Session) -> User:
        if self._user is None:
            self._user = nvs.exec(select(User).where(User.user_name == self.username)).one()
        return self._user

    def _sync_playlist(self, playlist: Playlist, nvs: Session) -> tuple[NVPlaylist, int, int]:
        nv_playlist = nvs.exec(select(NVPlaylist).where(NVPlaylist.name == playlist.name)).one_or_none()
        if not nv_playlist:
            nv_playlist = NVPlaylist(
                name=playlist.name,
                comment=playlist.description,
                owner_id=self._get_user(nvs).id,
            )
            nvs.add(nv_playlist)
            nvs.flush()

        tracks_stmt = (
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        )

        added = total = 0
        for pt in self.s.exec(tracks_stmt):
            total += 1
            nv_pt = self._make_nv_playlist_track(pt.track, nv_playlist, nvs)
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

    def _make_nv_playlist_track(
        self, track: Track, nv_playlist: NVPlaylist, nvs: Session
    ) -> PlaylistTracks | None:
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
