from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from airdrome.console import console
from airdrome.library import MAIN_SUBDIR
from airdrome.models import Playlist, PlaylistSyncState, PlaylistTrack, Track, TrackFile

from ..models import MediaFile, Playlist as NVPlaylist, PlaylistTracks, User, get_nv_engine


def _three_way_merge(base: list[int], ours: list[int], theirs: list[int]) -> list[int]:
    """Merge two ordered track-ID lists against a common base snapshot.

    Removals from either side are respected. Additions from *theirs* (Navidrome)
    are appended after *ours* (Airdrome). Airdrome ordering is authoritative.
    """
    base_set = set(base)
    theirs_set = set(theirs)
    theirs_removed = base_set - theirs_set
    theirs_added = theirs_set - base_set

    merged = [t for t in ours if t not in theirs_removed]
    merged_set = set(merged)
    for t in theirs:
        if t in theirs_added and t not in merged_set:
            merged.append(t)
            merged_set.add(t)
    return merged


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
        """Merge every canonical Airdrome playlist into Navidrome (3-way merge)."""
        self._sync_all()

    def pull_playlists(self):
        """Merge Navidrome playlist changes back into Airdrome (3-way merge)."""
        self._sync_all()

    def _sync_all(self):
        with Session(get_nv_engine()) as nvs:
            all_playlists = list(self.s.exec(select(Playlist).order_by(Playlist.name)))
            changed = 0
            for playlist in all_playlists:
                had_changes = self._sync_one(playlist, nvs)
                marker = "[green]+[/green]" if had_changes else "[dim]=[/dim]"
                console.print(f"  {marker}  {playlist.name}")
                if had_changes:
                    changed += 1
            nvs.commit()
            self.s.flush()
        console.print(f"[green]{changed}/{len(all_playlists)} playlists updated[/green]")

    def _sync_one(self, playlist: Playlist, nvs: Session) -> bool:
        """3-way merge one Airdrome playlist with its Navidrome counterpart.

        Returns True if any changes were applied to either side.
        """
        airdrome_ids = self._airdrome_canonical_ids(playlist)
        nv_playlist = self._get_or_create_nv_playlist(playlist, nvs)
        nv_ids = self._nv_canonical_ids(nv_playlist, nvs)

        sync_state = self.s.exec(
            select(PlaylistSyncState).where(PlaylistSyncState.playlist_id == playlist.id)
        ).one_or_none()
        base = sync_state.synced_track_ids if sync_state else []

        merged = _three_way_merge(base, airdrome_ids, nv_ids)

        changed = merged != airdrome_ids or merged != nv_ids
        if changed:
            # nv_synced contains only the tracks that actually landed in NV (some
            # may lack a matching MediaFile). Saving only those in the snapshot
            # keeps unresolved tracks invisible to the merge, so they stay in
            # Airdrome and get retried on the next sync instead of being treated
            # as "deleted from NV".
            nv_synced = self._apply_to_nv(playlist, nv_playlist, merged, nvs)
            self._apply_to_airdrome(playlist, merged)
        else:
            nv_synced = nv_ids

        self._save_state(sync_state, playlist, nv_playlist, nv_synced)
        return changed

    # ── helpers ──────────────────────────────────────────────────────────────

    def _get_user(self, nvs: Session) -> User:
        return nvs.exec(select(User).where(User.user_name == self.username)).one()

    def _get_or_create_nv_playlist(self, playlist: Playlist, nvs: Session) -> NVPlaylist:
        nv_playlist = nvs.exec(select(NVPlaylist).where(NVPlaylist.name == playlist.name)).one_or_none()
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
        return nv_playlist

    def _airdrome_canonical_ids(self, playlist: Playlist) -> list[int]:
        """Return ordered canonical track IDs for this Airdrome playlist."""
        rows = self.s.exec(
            select(PlaylistTrack)
            .where(PlaylistTrack.playlist_id == playlist.id)
            .order_by(PlaylistTrack.position)
        ).all()
        return [pt.track.canon_id or pt.track.id for pt in rows]

    def _nv_canonical_ids(self, nv_playlist: NVPlaylist, nvs: Session) -> list[int]:
        """Map Navidrome playlist tracks to canonical Airdrome track IDs."""
        result = []
        pts = nvs.exec(
            select(PlaylistTracks)
            .where(PlaylistTracks.playlist_id == nv_playlist.id)
            .order_by(PlaylistTracks.id)
        ).all()
        for pt in pts:
            media_file = nvs.get(MediaFile, pt.media_file_id)
            if not media_file:
                continue
            tf = self.s.exec(
                select(TrackFile).where(TrackFile.library_path == f"{MAIN_SUBDIR}/{media_file.path}")
            ).one_or_none()
            if not tf or not tf.track_id:
                continue
            track = self.s.get(Track, tf.track_id)
            if track:
                result.append(track.canon_id or track.id)
        return result

    def _apply_to_nv(
        self, playlist: Playlist, nv_playlist: NVPlaylist, merged_ids: list[int], nvs: Session
    ) -> list[int]:
        """Rebuild the NV playlist from merged_ids. Returns only the IDs that had a matching MediaFile."""
        nvs.exec(delete(PlaylistTracks).where(PlaylistTracks.playlist_id == nv_playlist.id))
        nvs.flush()

        nv_pos = total = 0
        total_duration = total_size = 0.0
        placed_ids: list[int] = []
        for track_id in merged_ids:
            track = self.s.get(Track, track_id)
            if not track or not track.main_file or not track.main_file.navidrome_path:
                continue
            media_file = nvs.exec(
                select(MediaFile).where(MediaFile.path == track.main_file.navidrome_path)
            ).one_or_none()
            if not media_file:
                console.print(f"[yellow]not found in Navidrome: {track.main_file.navidrome_path}[/yellow]")
                continue
            nv_pos += 1
            total += 1
            total_duration += media_file.duration or 0
            total_size += media_file.size or 0
            nvs.add(PlaylistTracks(id=nv_pos, playlist_id=nv_playlist.id, media_file_id=media_file.id))
            placed_ids.append(track_id)

        nvs.flush()
        nv_playlist.song_count = total
        nv_playlist.duration = total_duration
        nv_playlist.size = int(total_size)
        nv_playlist.updated_at = datetime.now(timezone.utc)
        nvs.flush()
        return placed_ids

    def _apply_to_airdrome(self, playlist: Playlist, merged_ids: list[int]):
        self.s.exec(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id))
        self.s.flush()
        for pos, track_id in enumerate(merged_ids, start=1):
            self.s.add(PlaylistTrack(playlist_id=playlist.id, track_id=track_id, position=pos))
        self.s.flush()

    def _save_state(
        self,
        sync_state: PlaylistSyncState | None,
        playlist: Playlist,
        nv_playlist: NVPlaylist,
        merged_ids: list[int],
    ):
        now = datetime.now(timezone.utc)
        if sync_state:
            sync_state.synced_track_ids = merged_ids
            sync_state.nv_playlist_id = nv_playlist.id
            sync_state.synced_at = now
        else:
            self.s.add(
                PlaylistSyncState(
                    playlist_id=playlist.id,
                    nv_playlist_id=nv_playlist.id,
                    synced_track_ids=merged_ids,
                    synced_at=now,
                )
            )
        self.s.flush()
