from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine, Relationship

from .enums import Platform, TrackType, Kind


class TrackPlaylistLink(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="track.id", index=True)
    playlist_id: int = Field(foreign_key="playlist.id", index=True)
    added_at: datetime = Field(default_factory=datetime.now)


class Playlist(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("playlist_id", name="uq_playlist_playlist_id"),
        UniqueConstraint("persistent_id", name="uq_playlist_persistent_id"),
    )
    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int
    persistent_id: str
    name: str
    description: str
    all_items: bool
    parent_persistent_id: str | None = Field(None)

    master: bool = Field(False)
    visible: bool = Field(True)
    music: bool = Field(False)
    folder: bool = Field(False)
    distinguished_kind: int | None = Field(None)
    favorited: bool = Field(False)
    loved: bool = Field(False)

    tracks: list[Track] = Relationship(back_populates="playlists", link_model=TrackPlaylistLink)


class Track(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("track_id", name="uq_track_track_id"),
        UniqueConstraint("persistent_id", name="uq_track_persistent_id"),
    )
    id: int | None = Field(default=None, primary_key=True)
    plays: list["TrackPlay"] = Relationship(back_populates="track")
    playlists: list["Playlist"] = Relationship(back_populates="tracks", link_model=TrackPlaylistLink)

    # Apple Music data
    track_id: int = Field(unique=True)
    track_type: TrackType = Field(sa_column=sa.Column(sa.Enum(TrackType, native_enum=False)))
    name: str
    name_norm: str
    persistent_id: str = Field(unique=True)
    size: int

    apple_music: bool = Field(False)
    """True means the track was added from Apple Music, and not from the local library"""

    artist: str | None = Field(None)
    """Unknown Artist in Explorer"""
    artist_norm: str | None = Field(None)
    album_artist: str | None = Field(None)
    album: str | None = Field(None)
    """Unknown Album in Explorer"""
    album_norm: str | None = Field(None)
    track_number: int | None = Field(None)
    date_added: datetime | None = Field(None)
    year: int | None = Field(None)
    release_date: datetime | None = Field(None)

    kind: Kind | None = Field(None)
    total_time: int | None = Field(None)
    rating: int | None = Field(None)
    rating_computed: bool = Field(False)
    album_rating: int | None = Field(None)
    album_rating_computed: bool = Field(False)
    music_video: bool = Field(False)
    has_video: bool = Field(False)
    file_folder_count: int | None = Field(None)
    library_folder_count: int | None = Field(None)
    grouping: str | None = Field(None)
    """An old field I've used to mark full albums"""
    genre: str | None = Field(None)
    location: str | None = Field(None)
    """MacOS path (since we only can export the XML from Mac now)"""
    date_modified: datetime | None = Field(None)
    protected: bool = Field(False)
    # extra fields
    comments: str | None = Field(None)
    disc_number: int | None = Field(None)
    play_count: int | None = Field(None)
    play_date: datetime | None = Field(None)
    play_date_utc: datetime | None = Field(None)
    sort_name: str | None = Field(None)
    sort_artist: str | None = Field(None)
    sort_album_artist: str | None = Field(None)
    sort_album: str | None = Field(None)
    sort_composer: str | None = Field(None)
    album_loved: bool = Field(False)
    disc_count: int | None = Field(None)
    track_count: int | None = Field(None)
    sample_rate: int | None = Field(None)
    skip_count: int | None = Field(None)
    skip_date: datetime | None = Field(None)
    work: str | None = Field(None)
    composer: str | None = Field(None)
    loved: bool = Field(False)
    favorited: bool = Field(False)
    """Alias for `loved`"""
    part_of_gapless_album: bool | None = Field(None)
    purchased: bool = Field(False)
    """True for kind=Купленное аудио AAC"""
    matched: bool = Field(False)
    compilation: bool | None = Field(None)
    """Has "Compilations" for Artist folder."""
    explicit: bool = Field(False)
    normalization: int | None = Field(None)
    hd: bool = Field(False)
    """For video files"""
    volume_adjustment: int | None = Field(None)
    movement_name: str | None = Field(None)
    movement_count: int | None = Field(None)
    disliked: bool = Field(False)

    # not goes into a database
    playlist_only: bool = Field(False)
    """Apple Music tracks, that are a part of a playlist but not in the library"""
    artwork_count: int | None = Field(None)
    bit_rate: int | None = Field(None)
    bpm: int | None = Field(None)
    clean: bool = Field(False)

    files: list["TrackFile"] = Relationship(back_populates="track")

    @property
    def short_info(self):
        tn = self.track_number if self.track_number is not None else "-"
        cloud = "cloud" if self.apple_music else "local"
        added = self.date_added.strftime("%Y-%m-%d %H:%M:%S")
        return f"[{self.track_id}] {self.artist} ({self.album}) {tn}. {self.name} added {added} [{cloud}]"


class TrackFile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="track.id", index=True, ondelete="CASCADE")
    track: Track = Relationship(back_populates="files")

    path: str


class TrackPlay(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("platform", "date", name="uq_trackplay_platform_date"),)

    id: int | None = Field(default=None, primary_key=True)

    date: datetime
    platform: Platform

    track_id: int | None = Field(foreign_key="track.id", default=None, index=True)
    track: Track = Relationship(back_populates="plays")


engine = create_engine("postgresql+psycopg://postgres:postgres@localhost:5437/postgres", echo=False)
