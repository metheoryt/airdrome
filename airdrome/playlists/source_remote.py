"""Read-only `PlaylistAdapter` over an imported cloud source.

A cloud source (Apple iTunes XML, Apple Media Services, …) has no write API, so its
"current state" is the `SourcePlaylist`/`SourceTrack` rows the last import wrote. This
adapter exposes those rows through the same interface the reconcile engine drives for
backends, so a source becomes just another — read-only — remote. The write half raises:
the engine must never call it for a `writable = False` remote.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.enums import Source
from airdrome.models import Track

from .adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter


class SourcePlaylistRemote(PlaylistAdapter):
    """Reconcile remote backed by one source provider's imported playlists.

    Scoped to a single `provider` (e.g. `apple_xml`) — the two Apple exports are distinct
    remotes with their own bases. External ids are the source's native ids: a
    `SourcePlaylist.source_id` for playlists, a stringified `SourceTrack.id` for track refs.
    """

    writable = False

    def __init__(self, session: Session, provider: Source):
        self._s = session
        self.remote = provider  # the source provider IS this remote's identity

    # source remotes only read the local DB — no external session to manage.
    def __enter__(self) -> SourcePlaylistRemote:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def _playlist(self, external_id: str) -> SourcePlaylist | None:
        return self._s.scalars(
            select(SourcePlaylist).where(
                SourcePlaylist.provider == self.remote,
                SourcePlaylist.source_id == external_id,
                ~SourcePlaylist.folder,
            )
        ).one_or_none()

    # ── read interface ────────────────────────────────────────────────────────

    def list_playlists(self) -> list[ExternalPlaylist]:
        rows = self._s.scalars(
            select(SourcePlaylist).where(SourcePlaylist.provider == self.remote, ~SourcePlaylist.folder)
        ).all()
        return [ExternalPlaylist(id=p.source_id, name=p.name, comment=p.description) for p in rows]

    def get(self, external_id: str) -> ExternalPlaylist | None:
        pl = self._playlist(external_id)
        if pl is None:
            return None
        return ExternalPlaylist(id=pl.source_id, name=pl.name, comment=pl.description)

    def get_track_refs(self, external_id: str) -> list[ExternalTrackRef]:
        pl = self._playlist(external_id)
        if pl is None:
            return []
        members = sorted(pl.members, key=lambda m: m.position)
        return [ExternalTrackRef(id=str(m.track_id)) for m in members]

    def to_canonical_track(self, ref: ExternalTrackRef) -> int | None:
        st = self._s.get(SourceTrack, int(ref.id))
        if st is None or st.track_id is None:
            return None
        track = self._s.get(Track, st.track_id)
        if track is None:
            return None
        return track.canon_id or track.id

    # ── write interface (never called for a read-only remote) ─────────────────

    def create(self, playlist) -> ExternalPlaylist:
        raise NotImplementedError("source remotes are read-only")

    def add_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        raise NotImplementedError("source remotes are read-only")

    def remove_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        raise NotImplementedError("source remotes are read-only")

    def from_canonical_track(self, track_id: int) -> ExternalTrackRef | None:
        raise NotImplementedError("source remotes are read-only")
