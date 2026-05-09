import sqlalchemy as sa
from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship

from airdrome.models import AwareDatetime, Base, Track

from .mixins import AppleFSDiscoverable


class AppleMSTrack(Base, AppleFSDiscoverable, table=True):
    __tablename__ = "apple_ms_track"
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = Field(default=None, primary_key=True)

    track_id: int | None = Field(None, foreign_key="track.id", ondelete="CASCADE")
    track: Track | None = Relationship(back_populates="apple_ms_tracks")

    playlist_memberships: list["AppleMSPlaylistTrack"] = Relationship(back_populates="track")

    track_identifier: int = Field(sa_column=sa.Column(sa.BIGINT, unique=True, nullable=False))
    title: str
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    compilation: bool = False
    track_number: int | None = None
    disc_number: int | None = None
    track_count: int | None = None
    disc_count: int | None = None
    year: int | None = None
    duration: int | None = None  # milliseconds
    play_count: int | None = None
    skip_count: int | None = None
    date_added: AwareDatetime | None = None
    date_modified: AwareDatetime | None = None
    release_date: AwareDatetime | None = None
    genre: str | None = None
    audio_file_extension: str | None = None
    is_purchased: bool = False
    purchased_track_identifier: int | None = Field(None, sa_column=sa.Column(sa.BIGINT, nullable=True))
    audio_matched_track_identifier: int | None = Field(None, sa_column=sa.Column(sa.BIGINT, nullable=True))


class AppleMSPlaylist(Base, table=True):
    __tablename__ = "apple_ms_playlist"
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = Field(default=None, primary_key=True)
    container_identifier: int = Field(sa_column=sa.Column(sa.BIGINT, unique=True, nullable=False))
    title: str
    container_type: str
    parent_folder_identifier: int | None = Field(None, sa_column=sa.Column(sa.BIGINT, nullable=True))
    date_added: AwareDatetime | None = None
    items_modified_date: AwareDatetime | None = None

    members: list["AppleMSPlaylistTrack"] = Relationship(back_populates="playlist")


class AppleMSPlaylistTrack(Base, table=True):
    __tablename__ = "apple_ms_playlist_track"
    __table_args__ = (UniqueConstraint("track_id", "playlist_id", name="uq_apple_ms_playlist_track"),)

    id: int | None = Field(default=None, primary_key=True)

    playlist_id: int = Field(foreign_key="apple_ms_playlist.id", index=True, ondelete="CASCADE")
    playlist: AppleMSPlaylist = Relationship(back_populates="members")

    track_id: int = Field(foreign_key="apple_ms_track.id", index=True, ondelete="CASCADE")
    track: AppleMSTrack = Relationship(back_populates="playlist_memberships")

    position: int = Field(index=True)
