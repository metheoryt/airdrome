from sqlmodel import Session, delete, func, select

from airdrome.console import console
from airdrome.models import Playlist, PlaylistTrack, Track

from ..models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


class NVPlaylistSyncer:
    def __init__(self, s: Session, username: str):
        self.s = s
        self.username = username

    def drop_navi_playlists(self):
        """Drop Navidrome playlists whose names match airdrome playlists."""
        airdrome_names = set(self.s.exec(select(Playlist.name)).all())
        if not airdrome_names:
            return
        with Session(get_nv_engine()) as nvs:
            user = self._get_user(nvs)
            playlists = nvs.exec(
                select(NVPlaylist).where(
                    NVPlaylist.owner_id == user.id,
                    NVPlaylist.name.in_(airdrome_names),
                )
            ).all()
            ids = [p.id for p in playlists]
            if ids:
                nvs.exec(delete(PlaylistTracks).where(PlaylistTracks.playlist_id.in_(ids)))
                nvs.exec(delete(NVPlaylist).where(NVPlaylist.id.in_(ids)))
                nvs.commit()
                console.print(f"[yellow]Dropped {len(ids)} existing playlists[/yellow]")

    def push_playlists(self):
        """Push every canonical Playlist 1:1 to Navidrome. Safe to rerun."""
        with Session(get_nv_engine()) as nvs:
            all_playlists = list(self.s.exec(select(Playlist).order_by(Playlist.name)))
            synced = 0
            for playlist in all_playlists:
                _, added, total = self._push_playlist(playlist, nvs)
                console.print(f"  [cyan]{added}/{total}[/cyan]  {playlist.name}")
                if added > 0:
                    synced += 1
            nvs.commit()
            console.print(f"[green]{synced} playlists with new tracks[/green]")

    def _get_user(self, nvs: Session) -> User:
        return nvs.exec(select(User).where(User.user_name == self.username)).one()

    def _push_playlist(self, playlist: Playlist, nvs: Session) -> tuple[NVPlaylist, int, int]:
        nv_playlist: NVPlaylist | None = nvs.exec(
            select(NVPlaylist).where(NVPlaylist.name == playlist.name)
        ).one_or_none()

        if not nv_playlist:
            nv_playlist = NVPlaylist(
                name=playlist.name,
                owner_id=self._get_user(nvs).id,
                comment=playlist.comment,
                created_at=playlist.date_added,
                updated_at=playlist.date_modified,
            )
            nvs.add(nv_playlist)
            nvs.flush()

        elif nv_playlist.updated_at > playlist.date_modified:
            # Navidrome playlist is newer or the same, skip
            return nv_playlist, 0, 0

        else:
            # Navidrome playlist is older, update
            nv_playlist.comment = playlist.comment
            nv_playlist.updated_at = playlist.date_modified
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
        effective_track = track.canon if track.canon_id else track
        if not effective_track or not effective_track.main_file:
            return None

        media_file = nvs.exec(
            select(MediaFile).where(MediaFile.path == effective_track.main_file.navidrome_path)
        ).one_or_none()
        if not media_file:
            console.print(
                f"[yellow]not found in Navidrome: {effective_track.main_file.navidrome_path}[/yellow]"
            )
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
