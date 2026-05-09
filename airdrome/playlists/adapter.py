"""Backend-agnostic playlist adapter interface.

Each music server (Navidrome, Jellyfin, Plex, ...) provides a `PlaylistAdapter`
that translates between Airdrome's canonical `Track`/`Playlist` model and the
server's own playlist representation. The merge engine in `sync.py` is written
against this interface and never touches a backend's tables directly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

from airdrome.models import Backend, Playlist


@dataclass(frozen=True)
class ExternalTrackRef:
    """Opaque handle to a track inside a backend playlist.

    `id` is the backend-specific media identifier (Navidrome's media_file_id,
    Plex's ratingKey, ...). Adapters can subclass to carry extra fields needed
    for write-back (e.g. the row id of the playlist_tracks association).
    """

    id: str


@dataclass(frozen=True)
class ExternalPlaylist:
    id: str
    name: str
    comment: str | None = None


class PlaylistAdapter(ABC):
    """Translates between Airdrome canonical playlists and a specific backend."""

    backend: Backend

    @abstractmethod
    def list_playlists(self) -> Iterable[ExternalPlaylist]:
        """All playlists owned by the configured user on the backend."""

    @abstractmethod
    def get(self, external_id: str) -> ExternalPlaylist | None:
        """Look up a playlist by its backend ID. None if it no longer exists."""

    @abstractmethod
    def create(self, playlist: Playlist) -> ExternalPlaylist:
        """Create a backend playlist mirroring an Airdrome canonical playlist."""

    @abstractmethod
    def get_track_refs(self, external_id: str) -> list[ExternalTrackRef]:
        """Tracks currently in the backend playlist, in playback order."""

    @abstractmethod
    def add_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        """Append `ref` to the end of the backend playlist."""

    @abstractmethod
    def remove_track(self, external_id: str, ref: ExternalTrackRef) -> None:
        """Remove `ref` from the backend playlist."""

    @abstractmethod
    def to_canonical_track(self, ref: ExternalTrackRef) -> int | None:
        """Map a backend track ref to a canonical Airdrome `Track.id`.

        Returns None when no Airdrome track corresponds to this backend track
        (e.g. the file isn't indexed in Airdrome). The merge engine treats
        such tracks as backend-only and leaves them untouched.
        """

    @abstractmethod
    def from_canonical_track(self, track_id: int) -> ExternalTrackRef | None:
        """Map a canonical Airdrome `Track.id` to a backend track ref.

        Returns None when the track has no backend representation yet (no
        local file, or the file hasn't been indexed by the backend). The
        merge engine treats such tracks as Airdrome-only and skips writing.
        """

    def commit(self) -> None:
        """Flush pending writes to the backend. Called after each playlist is synced."""

    def rollback(self) -> None:
        """Discard pending writes to the backend on error."""
