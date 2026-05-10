import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from airdrome.models import AwareDatetime, Base, Track

from ..enums import Kind, TrackType
from . import AppleFSDiscoverable


_TRACK_ALIAS_MAP = {
    "Track ID": "apple_track_id",
    "Name": "name",
    "Album": "album",
    "Artist": "artist",
    "Album Artist": "album_artist",
    "Apple Music": "apple_music",
    "Compilation": "compilation",
    "Track Number": "track_number",
    "Disc Number": "disc_number",
    "Year": "year",
    "Release Date": "release_date",
    "Loved": "loved",
    "Favorited": "favorited",
    "Rating": "rating",
    "Rating Computed": "rating_computed",
    "Album Loved": "album_loved",
    "Album Rating": "album_rating",
    "Album Rating Computed": "album_rating_computed",
    "Date Added": "date_added",
    "Date Modified": "date_modified",
    "Play Date UTC": "play_date_utc",
    "Play Date": "play_date",
    "Total Time": "total_time",
    "Size": "size",
    "Track Type": "track_type",
    "Persistent ID": "persistent_id",
    "Kind": "kind",
    "Grouping": "grouping",
    "Genre": "genre",
    "Location": "location",
    "Bit Rate": "bit_rate",
    "Sample Rate": "sample_rate",
    "BPM": "bpm",
    "Normalization": "normalization",
    "Volume Adjustment": "volume_adjustment",
    "Play Count": "play_count",
    "Skip Count": "skip_count",
    "Skip Date": "skip_date",
    "Disliked": "disliked",
    "Comments": "comments",
    "Sort Name": "sort_name",
    "Sort Artist": "sort_artist",
    "Sort Album Artist": "sort_album_artist",
    "Sort Album": "sort_album",
    "Sort Composer": "sort_composer",
    "Work": "work",
    "Composer": "composer",
    "Movement Name": "movement_name",
    "Movement Count": "movement_count",
    "Disc Count": "disc_count",
    "Track Count": "track_count",
    "Artwork Count": "artwork_count",
    "File Folder Count": "file_folder_count",
    "Library Folder Count": "library_folder_count",
    "Protected": "protected",
    "Music Video": "music_video",
    "Has Video": "has_video",
    "Part Of Gapless Album": "part_of_gapless_album",
    "Playlist Only": "playlist_only",
    "Purchased": "purchased",
    "Matched": "matched",
    "Explicit": "explicit",
    "Clean": "clean",
    "HD": "hd",
}


class AppleTrack(Base, AppleFSDiscoverable):
    __tablename__ = "appletrack"

    id: Mapped[int | None] = mapped_column(primary_key=True)

    track_id: Mapped[int | None] = mapped_column(sa.ForeignKey("track.id", ondelete="CASCADE"))
    track: Mapped[Track | None] = relationship(back_populates="apple_tracks")

    playlist_memberships: Mapped[list["ApplePlaylistTrack"]] = relationship(back_populates="track")

    # Apple Music data
    apple_track_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str]

    @property
    def title(self) -> str:
        # for AppleFSDiscoverable
        return self.name

    album: Mapped[str | None]
    """Unknown Album in Explorer"""
    artist: Mapped[str | None]
    """Unknown Artist in Explorer"""
    album_artist: Mapped[str | None]

    apple_music: Mapped[bool] = mapped_column(default=False)
    """True means the track was added from Apple Music, and not from the local library"""

    compilation: Mapped[bool | None]
    """Has "Compilations" for Artist folder."""
    track_number: Mapped[int | None]
    disc_number: Mapped[int | None]
    year: Mapped[int | None]
    release_date: Mapped[AwareDatetime | None]
    loved: Mapped[bool] = mapped_column(default=False)
    favorited: Mapped[bool] = mapped_column(default=False)
    """Alias for `loved`"""
    rating: Mapped[int | None]
    rating_computed: Mapped[bool] = mapped_column(default=False)
    album_loved: Mapped[bool] = mapped_column(default=False)
    album_rating: Mapped[int | None]
    album_rating_computed: Mapped[bool] = mapped_column(default=False)
    date_added: Mapped[AwareDatetime]
    date_modified: Mapped[AwareDatetime]
    play_date_utc: Mapped[AwareDatetime | None]
    play_date: Mapped[int | None] = mapped_column(sa.BIGINT, nullable=True)
    total_time: Mapped[int | None]
    size: Mapped[int]
    track_type: Mapped[TrackType] = mapped_column(sa.Enum(TrackType, native_enum=False))
    persistent_id: Mapped[str] = mapped_column(unique=True)

    kind: Mapped[Kind | None]
    grouping: Mapped[str | None]
    """An old field I've used to mark full albums"""
    genre: Mapped[str | None]
    location: Mapped[str | None]
    """MacOS path (since we only can export the XML from Mac now)"""

    # extra fields
    bit_rate: Mapped[int | None]
    sample_rate: Mapped[int | None]
    bpm: Mapped[int | None]
    normalization: Mapped[int | None]
    volume_adjustment: Mapped[int | None]

    play_count: Mapped[int | None]
    skip_count: Mapped[int | None]
    skip_date: Mapped[AwareDatetime | None]
    disliked: Mapped[bool] = mapped_column(default=False)
    comments: Mapped[str | None]
    sort_name: Mapped[str | None]
    sort_artist: Mapped[str | None]
    sort_album_artist: Mapped[str | None]
    sort_album: Mapped[str | None]
    sort_composer: Mapped[str | None]
    work: Mapped[str | None]
    composer: Mapped[str | None]
    movement_name: Mapped[str | None]
    movement_count: Mapped[int | None]
    disc_count: Mapped[int | None]
    track_count: Mapped[int | None]
    artwork_count: Mapped[int | None]

    file_folder_count: Mapped[int | None]
    library_folder_count: Mapped[int | None]
    protected: Mapped[bool] = mapped_column(default=False)
    music_video: Mapped[bool] = mapped_column(default=False)
    has_video: Mapped[bool] = mapped_column(default=False)
    part_of_gapless_album: Mapped[bool | None]
    playlist_only: Mapped[bool] = mapped_column(default=False)
    """Apple Music tracks, that are a part of a playlist but not in the library"""
    purchased: Mapped[bool] = mapped_column(default=False)
    """True for kind=Купленное аудио AAC"""
    matched: Mapped[bool] = mapped_column(default=False)
    explicit: Mapped[bool] = mapped_column(default=False)
    clean: Mapped[bool] = mapped_column(default=False)
    hd: Mapped[bool] = mapped_column(default=False)
    """For video files"""

    @classmethod
    def from_raw(cls, data: dict) -> "AppleTrack":
        """Construct from a raw Apple XML track dict (field names use Apple's aliases)."""
        mapped = {_TRACK_ALIAS_MAP.get(k, k): v for k, v in data.items()}
        cols = {c.key for c in cls.__table__.columns}
        return cls(**{k: v for k, v in mapped.items() if k in cols})


class ApplePlaylistBase(BaseModel):
    """Pydantic model for Apple playlist import data — shared with ApplePlaylistImport schema."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias="Name")
    playlist_id: int = Field(alias="Playlist ID")
    persistent_id: str = Field(alias="Playlist Persistent ID")
    description: str = Field(alias="Description")
    all_items: bool = Field(alias="All Items")
    parent_persistent_id: str | None = Field(None, alias="Parent Persistent ID")

    master: bool = Field(False, alias="Master")
    visible: bool = Field(True, alias="Visible")
    music: bool = Field(False, alias="Music")
    folder: bool = Field(False, alias="Folder")
    distinguished_kind: int | None = Field(None, alias="Distinguished Kind")
    favorited: bool = Field(False, alias="Favorited")
    loved: bool = Field(False, alias="Loved")


class ApplePlaylist(Base):
    __tablename__ = "appleplaylist"

    id: Mapped[int | None] = mapped_column(primary_key=True)
    name: Mapped[str]
    playlist_id: Mapped[int] = mapped_column(unique=True)
    persistent_id: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str]
    all_items: Mapped[bool]
    parent_persistent_id: Mapped[str | None]
    master: Mapped[bool] = mapped_column(default=False)
    visible: Mapped[bool] = mapped_column(default=True)
    music: Mapped[bool] = mapped_column(default=False)
    folder: Mapped[bool] = mapped_column(default=False)
    distinguished_kind: Mapped[int | None]
    favorited: Mapped[bool] = mapped_column(default=False)
    loved: Mapped[bool] = mapped_column(default=False)

    members: Mapped[list["ApplePlaylistTrack"]] = relationship(back_populates="playlist")


class ApplePlaylistTrack(Base):
    __tablename__ = "appleplaylisttrack"
    __table_args__ = (
        UniqueConstraint("track_id", "playlist_id", name="uq_trackplaylistlink_track_id_playlist_id"),
    )

    id: Mapped[int | None] = mapped_column(primary_key=True)

    track_id: Mapped[int] = mapped_column(sa.ForeignKey("appletrack.id", ondelete="CASCADE"), index=True)
    track: Mapped[AppleTrack] = relationship(back_populates="playlist_memberships")

    playlist_id: Mapped[int] = mapped_column(
        sa.ForeignKey("appleplaylist.id", ondelete="CASCADE"), index=True
    )
    playlist: Mapped[ApplePlaylist] = relationship(back_populates="members")

    position: Mapped[int] = mapped_column(index=True)
