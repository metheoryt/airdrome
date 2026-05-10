import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from airdrome.models import AwareDatetime, Base, Track

from .mixins import AppleFSDiscoverable


class AppleMSTrack(Base, AppleFSDiscoverable):
    __tablename__ = "apple_ms_track"

    id: Mapped[int] = mapped_column(primary_key=True)

    track_id: Mapped[int | None] = mapped_column(sa.ForeignKey("track.id", ondelete="CASCADE"))
    track: Mapped[Track | None] = relationship(back_populates="apple_ms_tracks")

    playlist_memberships: Mapped[list["AppleMSPlaylistTrack"]] = relationship(back_populates="track")

    track_identifier: Mapped[int] = mapped_column(sa.BIGINT, unique=True, nullable=False)
    title: Mapped[str]
    artist: Mapped[str | None]
    album: Mapped[str | None]
    album_artist: Mapped[str | None]
    compilation: Mapped[bool] = mapped_column(default=False)
    track_number: Mapped[int | None]
    disc_number: Mapped[int | None]
    track_count: Mapped[int | None]
    disc_count: Mapped[int | None]
    year: Mapped[int | None]
    duration: Mapped[int | None]  # milliseconds
    play_count: Mapped[int | None]
    skip_count: Mapped[int | None]
    date_added: Mapped[AwareDatetime | None]
    date_modified: Mapped[AwareDatetime | None]
    release_date: Mapped[AwareDatetime | None]
    genre: Mapped[str | None]
    audio_file_extension: Mapped[str | None]
    is_purchased: Mapped[bool] = mapped_column(default=False)
    purchased_track_identifier: Mapped[int | None] = mapped_column(sa.BIGINT, nullable=True)
    audio_matched_track_identifier: Mapped[int | None] = mapped_column(sa.BIGINT, nullable=True)


class AppleMSPlaylist(Base):
    __tablename__ = "apple_ms_playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    container_identifier: Mapped[int] = mapped_column(sa.BIGINT, unique=True, nullable=False)
    title: Mapped[str]
    container_type: Mapped[str]
    parent_folder_identifier: Mapped[int | None] = mapped_column(sa.BIGINT, nullable=True)
    date_added: Mapped[AwareDatetime | None]
    items_modified_date: Mapped[AwareDatetime | None]

    members: Mapped[list["AppleMSPlaylistTrack"]] = relationship(back_populates="playlist")


class AppleMSPlaylistTrack(Base):
    __tablename__ = "apple_ms_playlist_track"

    id: Mapped[int] = mapped_column(primary_key=True)

    playlist_id: Mapped[int] = mapped_column(
        sa.ForeignKey("apple_ms_playlist.id", ondelete="CASCADE"), index=True
    )
    playlist: Mapped[AppleMSPlaylist] = relationship(back_populates="members")

    track_id: Mapped[int] = mapped_column(sa.ForeignKey("apple_ms_track.id", ondelete="CASCADE"), index=True)
    track: Mapped[AppleMSTrack] = relationship(back_populates="playlist_memberships")

    position: Mapped[int] = mapped_column(index=True)
