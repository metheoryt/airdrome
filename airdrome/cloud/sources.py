"""Provider-agnostic source models.

A `SourceTrack`/`SourcePlaylist` is a track/playlist as seen by a single source export
(Apple iTunes XML, Apple Media Services, …). Common, queried fields live in real columns;
everything provider-specific is preserved verbatim in the `extra` JSONB blob. Each row carries
a `provider` marker and a `source_id` (the source's native identifier, stringified), and is later
unified into the canonical `Track`/`Playlist`.

Adding a new source is "a `Provider` value + a field-mapping table", not a new table.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from airdrome.enums import Provider
from airdrome.models import AwareDatetime, Base, Track

from .apple.models.mixins import AppleFSDiscoverable


def _json_safe(value: Any) -> Any:
    """Coerce a raw export value into something JSON/JSONB can store."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


class SourceTrack(Base, AppleFSDiscoverable):
    __tablename__ = "source_track"
    __table_args__ = (UniqueConstraint("provider", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[Provider] = mapped_column(sa.Enum(Provider, native_enum=False))
    source_id: Mapped[str]
    """The source's native id (apple_track_id / track_identifier), stringified."""

    track_id: Mapped[int | None] = mapped_column(ForeignKey("track.id", ondelete="CASCADE"), index=True)
    track: Mapped[Track | None] = relationship(back_populates="source_tracks")
    playlist_memberships: Mapped[list["SourcePlaylistTrack"]] = relationship(back_populates="track")

    # structured fields consumed downstream (matching, FS-path mixin, unify defaults)
    title: Mapped[str]
    artist: Mapped[str | None]
    album: Mapped[str | None]
    album_artist: Mapped[str | None]
    compilation: Mapped[bool | None]
    track_number: Mapped[int | None]
    disc_number: Mapped[int | None]
    year: Mapped[int | None]
    duration_ms: Mapped[int | None]
    date_added: Mapped[AwareDatetime | None]
    date_modified: Mapped[AwareDatetime | None]
    loved: Mapped[bool] = mapped_column(default=False)
    album_loved: Mapped[bool] = mapped_column(default=False)
    rating: Mapped[int | None]
    rating_computed: Mapped[bool] = mapped_column(default=False)
    album_rating: Mapped[int | None]
    album_rating_computed: Mapped[bool] = mapped_column(default=False)

    # provider-specific long tail (bpm, sort_*, kind, location, apple_music,
    # audio_file_extension, purchase ids, …) — preserved verbatim
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    @classmethod
    def _structured_keys(cls) -> set[str]:
        exclude = {"id", "track_id", "provider", "source_id", "extra"}
        return {c.key for c in cls.__table__.columns} - exclude

    @classmethod
    def from_raw(
        cls,
        provider: Provider,
        source_id: Any,
        data: dict,
        *,
        alias_map: dict[str, str],
    ) -> "SourceTrack":
        """Build from a raw export dict: rename keys via `alias_map`, split into structured
        columns vs. the `extra` blob. `source_id` is the source's native identifier."""
        mapped = {alias_map.get(k, k): v for k, v in data.items()}
        skeys = cls._structured_keys()
        structured = {k: v for k, v in mapped.items() if k in skeys}
        extra = {k: _json_safe(v) for k, v in mapped.items() if k not in skeys}
        return cls(provider=provider, source_id=str(source_id), extra=extra, **structured)


class SourcePlaylist(Base):
    __tablename__ = "source_playlist"
    __table_args__ = (UniqueConstraint("provider", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[Provider] = mapped_column(sa.Enum(Provider, native_enum=False))
    source_id: Mapped[str]

    name: Mapped[str]
    description: Mapped[str | None]
    date_added: Mapped[AwareDatetime | None]
    date_modified: Mapped[AwareDatetime | None]
    # kept structured: the unify gather filters folders out in SQL.
    # master/music (iTunes' internal Library/Music containers) are skipped at import instead.
    folder: Mapped[bool] = mapped_column(default=False)

    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    members: Mapped[list["SourcePlaylistTrack"]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan"
    )


class SourcePlaylistTrack(Base):
    __tablename__ = "source_playlist_track"

    id: Mapped[int] = mapped_column(primary_key=True)

    playlist_id: Mapped[int] = mapped_column(ForeignKey("source_playlist.id", ondelete="CASCADE"), index=True)
    playlist: Mapped[SourcePlaylist] = relationship(back_populates="members")

    track_id: Mapped[int] = mapped_column(ForeignKey("source_track.id", ondelete="CASCADE"), index=True)
    track: Mapped[SourceTrack] = relationship(back_populates="playlist_memberships")

    position: Mapped[int] = mapped_column(index=True)
