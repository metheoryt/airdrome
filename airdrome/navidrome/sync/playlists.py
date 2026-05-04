from itertools import groupby

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

    def push_navi_playlists(self):
        """
        For every canonical Playlist, get or create a Navidrome playlist and populate its tracks.
        Playlists with different names but identical track sets are skipped (first imported wins).
        Safe to rerun — existing entries are not duplicated.
        """
        with Session(get_nv_engine()) as nvs:
            imported_track_sets: set[frozenset[str]] = set()
            names_with_additions: set[str] = set()

            all_playlists = list(self.s.exec(select(Playlist).order_by(Playlist.name)))
            for name, group_iter in groupby(all_playlists, key=lambda p: p.name):
                group = list(group_iter)

                first_track_set = self._resolve_track_set(group[0], nvs)
                if first_track_set and first_track_set in imported_track_sets:
                    console.print(f"  [dim]{'skipped':<14}[/dim]  {name}")
                    continue
                if first_track_set:
                    imported_track_sets.add(first_track_set)
                _, first_added, first_total = self._sync_playlist(group[0], nvs)

                additional_added = 0
                for playlist in group[1:]:
                    track_set = self._resolve_track_set(playlist, nvs)
                    if track_set and track_set in imported_track_sets:
                        continue
                    if track_set:
                        imported_track_sets.add(track_set)
                    _, added, _ = self._sync_playlist(playlist, nvs)
                    additional_added += added

                base = f"{first_added}/{first_total}"
                suffix = f"(+{additional_added})" if additional_added > 0 else ""
                console.print(f"  [cyan]{base:<8}{suffix:<6}[/cyan]  {name}")
                if first_added + additional_added > 0:
                    names_with_additions.add(name)

            nvs.commit()
            console.print(f"[green]{len(names_with_additions)} playlists synced[/green]")

    def _get_user(self, nvs: Session) -> User:
        return nvs.exec(select(User).where(User.user_name == self.username)).one()

    def _resolve_track_set(self, playlist: Playlist, nvs: Session) -> frozenset[str]:
        """Returns frozenset of Navidrome media_file_ids for this playlist's matched tracks."""
        tracks_stmt = (
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        )
        ids: set[str] = set()
        for pt in self.s.exec(tracks_stmt):
            if not pt.track.main_file:
                continue
            media_file = nvs.exec(
                select(MediaFile).where(MediaFile.path == pt.track.main_file.navidrome_path)
            ).one_or_none()
            if media_file:
                ids.add(media_file.id)
        return frozenset(ids)

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
