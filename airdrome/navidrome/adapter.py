"""Navidrome implementation of `PlaylistAdapter`.

Owns the NV-side SQLite session for the duration of a sync run. All NV writes
go through this adapter; the merge engine never touches `MediaFile` or
`playlist_tracks` directly.
"""

from datetime import datetime, timezone
from typing import Iterable

from sqlmodel import Session, delete, func, select

from airdrome.console import console
from airdrome.library import MAIN_SUBDIR
from airdrome.models import Backend, Playlist, Track, TrackFile
from airdrome.playlists.adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter

from .models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


def _to_external(nv_pl: NVPlaylist) -> ExternalPlaylist:
    return ExternalPlaylist(id=nv_pl.id, name=nv_pl.name, comment=nv_pl.comment or None)


class NavidromeAdapter(PlaylistAdapter):
    backend = Backend.NAVIDROME

    def __init__(self, airdrome_session: Session, username: str):
        self._s = airdrome_session
        self._username = username
        self._nvs: Session | None = None
        self._user_id: str | None = None
        self._dirty_playlists: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self) -> "NavidromeAdapter":
        self._nvs = Session(get_nv_engine())
        user = self.nvs.exec(select(User).where(User.user_name == self._username)).one()
        self._user_id = user.id
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._nvs is not None
        self._nvs.close()
        self._nvs = None

    @property
    def nvs(self) -> Session:
        assert self._nvs is not None, "NavidromeAdapter must be used as a context manager"
        return self._nvs

    def commit(self) -> None:
        for nv_pl_id in self._dirty_playlists:
            self._recompute_totals(nv_pl_id)
        self._dirty_playlists.clear()
        self.nvs.commit()

    def rollback(self) -> None:
        self._dirty_playlists.clear()
        self.nvs.rollback()

    # ── playlist CRUD ────────────────────────────────────────────────────────

    def list_playlists(self) -> Iterable[ExternalPlaylist]:
        rows = self.nvs.exec(select(NVPlaylist).where(NVPlaylist.owner_id == self._user_id)).all()
        return [_to_external(p) for p in rows]

    def get(self, external_id: str) -> ExternalPlaylist | None:
        nv_pl = self.nvs.get(NVPlaylist, external_id)
        if nv_pl is None or nv_pl.owner_id != self._user_id:
            return None
        return _to_external(nv_pl)

    def create(self, playlist: Playlist) -> ExternalPlaylist:
        now = datetime.now(timezone.utc)
        nv_pl = NVPlaylist(
            name=playlist.name,
            owner_id=self._user_id,
            comment=playlist.comment,
            created_at=playlist.date_added or now,
            updated_at=playlist.date_modified or now,
        )
        self.nvs.add(nv_pl)
        self.nvs.flush()
        self._dirty_playlists.add(nv_pl.id)
        return _to_external(nv_pl)

    # ── track operations ─────────────────────────────────────────────────────

    def get_track_refs(self, external_id: str) -> list[ExternalTrackRef]:
        rows = self.nvs.exec(
            select(PlaylistTracks)
            .where(PlaylistTracks.playlist_id == external_id)
            .order_by(PlaylistTracks.id)
        ).all()
        return [ExternalTrackRef(id=pt.media_file_id) for pt in rows]

    def add_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        next_id = (
            self.nvs.exec(
                select(func.coalesce(func.max(PlaylistTracks.id), 0)).where(
                    PlaylistTracks.playlist_id == external_id
                )
            ).one()
            + 1
        )
        self.nvs.add(PlaylistTracks(id=next_id, playlist_id=external_id, media_file_id=ref.id))
        self.nvs.flush()
        self._dirty_playlists.add(external_id)

    def remove_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        self.nvs.exec(
            delete(PlaylistTracks).where(
                PlaylistTracks.playlist_id == external_id,
                PlaylistTracks.media_file_id == ref.id,
            )
        )
        self.nvs.flush()
        self._dirty_playlists.add(external_id)

    # ── canonical ↔ backend translation ──────────────────────────────────────

    def to_canonical_track(self, ref: ExternalTrackRef) -> int | None:
        media_file = self.nvs.get(MediaFile, ref.id)
        if media_file is None:
            return None
        tf = self._s.exec(
            select(TrackFile).where(TrackFile.library_path == f"{MAIN_SUBDIR}/{media_file.path}")
        ).one_or_none()
        if tf is None or tf.track_id is None:
            return None
        track = self._s.get(Track, tf.track_id)
        if track is None:
            return None
        return track.canon_id or track.id

    def from_canonical_track(self, track_id: int) -> ExternalTrackRef | None:
        track = self._s.get(Track, track_id)
        if track is None or track.main_file is None or track.main_file.navidrome_path is None:
            return None
        media_file = self.nvs.exec(
            select(MediaFile).where(MediaFile.path == track.main_file.navidrome_path)
        ).one_or_none()
        if media_file is None:
            console.print(f"[yellow]not found in Navidrome: {track.main_file.navidrome_path}[/yellow]")
            return None
        return ExternalTrackRef(id=media_file.id)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _recompute_totals(self, nv_pl_id: str) -> None:
        count, duration, size = self.nvs.exec(
            select(
                func.count(),
                func.coalesce(func.sum(MediaFile.duration), 0),
                func.coalesce(func.sum(MediaFile.size), 0),
            )
            .select_from(PlaylistTracks)
            .join(MediaFile, MediaFile.id == PlaylistTracks.media_file_id)
            .where(PlaylistTracks.playlist_id == nv_pl_id)
        ).one()
        nv_pl = self.nvs.get(NVPlaylist, nv_pl_id)
        if nv_pl is None:
            return
        nv_pl.song_count = count
        nv_pl.duration = duration
        nv_pl.size = int(size)
        nv_pl.updated_at = datetime.now(timezone.utc)
        self.nvs.flush()
