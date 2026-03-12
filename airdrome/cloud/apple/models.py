import itertools
from pathlib import Path

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship

from airdrome.models import AwareDatetime, Base, Track

from .enums import Kind, TrackType
from .schemas import PlaylistTrackSchema
from .utils import generate_path


class ApplePlaylistTrack(Base, table=True):
    __table_args__ = (
        UniqueConstraint("track_id", "playlist_id", name="uq_trackplaylistlink_track_id_playlist_id"),
    )

    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="appletrack.id", index=True, ondelete="CASCADE")
    track: AppleTrack = Relationship(back_populates="playlist_memberships")

    playlist_id: int = Field(foreign_key="appleplaylist.id", index=True, ondelete="CASCADE")
    playlist: ApplePlaylist = Relationship(back_populates="members")

    position: int = Field(index=True)


class ApplePlaylistBase(Base):
    name: str = Field(alias="Name")
    playlist_id: int = Field(unique=True, alias="Playlist ID")
    persistent_id: str = Field(unique=True, alias="Playlist Persistent ID")
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


class ApplePlaylistImport(ApplePlaylistBase):
    model_config = ConfigDict(extra="forbid")

    # don't put those into the database
    smart_info: bytes | None = Field(None, alias="Smart Info")
    smart_criteria: bytes | None = Field(None, alias="Smart Criteria")
    items: list[PlaylistTrackSchema] = Field(alias="Playlist Items")


class ApplePlaylist(ApplePlaylistBase, table=True):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: int | None = Field(default=None, primary_key=True)
    members: list[ApplePlaylistTrack] = Relationship(back_populates="playlist")


class AppleTrack(Base, table=True):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="track.id", ondelete="CASCADE")
    track: Track = Relationship(back_populates="apple_tracks")

    playlist_memberships: list["ApplePlaylistTrack"] = Relationship(back_populates="track")

    # Apple Music data
    apple_track_id: int = Field(unique=True, alias="Track ID")
    name: str = Field(alias="Name")
    album: str | None = Field(None, alias="Album")
    """Unknown Album in Explorer"""
    artist: str | None = Field(None, alias="Artist")
    """Unknown Artist in Explorer"""
    album_artist: str | None = Field(None, alias="Album Artist")

    apple_music: bool = Field(False, alias="Apple Music")
    """True means the track was added from Apple Music, and not from the local library"""

    compilation: bool | None = Field(None, alias="Compilation")
    """Has "Compilations" for Artist folder."""
    track_number: int | None = Field(None, alias="Track Number")
    disc_number: int | None = Field(None, alias="Disc Number")
    year: int | None = Field(None, alias="Year")
    release_date: AwareDatetime | None = Field(None, alias="Release Date")
    loved: bool = Field(False, alias="Loved")
    favorited: bool = Field(False, alias="Favorited")
    """Alias for `loved`"""
    rating: int | None = Field(None, alias="Rating")
    rating_computed: bool = Field(False, alias="Rating Computed")
    album_loved: bool = Field(False, alias="Album Loved")
    album_rating: int | None = Field(None, alias="Album Rating")
    album_rating_computed: bool = Field(False, alias="Album Rating Computed")
    date_added: AwareDatetime = Field(alias="Date Added")
    date_modified: AwareDatetime = Field(alias="Date Modified")
    play_date_utc: AwareDatetime | None = Field(None, alias="Play Date UTC")
    play_date: int | None = Field(None, sa_type=sa.BIGINT, alias="Play Date")
    total_time: int | None = Field(None, alias="Total Time")
    size: int = Field(alias="Size")
    track_type: TrackType = Field(
        sa_column=sa.Column(sa.Enum(TrackType, native_enum=False)), alias="Track Type"
    )
    persistent_id: str = Field(unique=True, alias="Persistent ID")

    kind: Kind | None = Field(None, alias="Kind")
    grouping: str | None = Field(None, alias="Grouping")
    """An old field I've used to mark full albums"""
    genre: str | None = Field(None, alias="Genre")
    location: str | None = Field(None, alias="Location")
    """MacOS path (since we only can export the XML from Mac now)"""

    # extra fields
    bit_rate: int | None = Field(None, alias="Bit Rate")
    sample_rate: int | None = Field(None, alias="Sample Rate")
    bpm: int | None = Field(None, alias="BPM")
    normalization: int | None = Field(None, alias="Normalization")
    volume_adjustment: int | None = Field(None, alias="Volume Adjustment")

    play_count: int | None = Field(None, alias="Play Count")
    skip_count: int | None = Field(None, alias="Skip Count")
    skip_date: AwareDatetime | None = Field(None, alias="Skip Date")
    disliked: bool = Field(False, alias="Disliked")
    comments: str | None = Field(None, alias="Comments")
    sort_name: str | None = Field(None, alias="Sort Name")
    sort_artist: str | None = Field(None, alias="Sort Artist")
    sort_album_artist: str | None = Field(None, alias="Sort Album Artist")
    sort_album: str | None = Field(None, alias="Sort Album")
    sort_composer: str | None = Field(None, alias="Sort Composer")
    work: str | None = Field(None, alias="Work")
    composer: str | None = Field(None, alias="Composer")
    movement_name: str | None = Field(None, alias="Movement Name")
    movement_count: int | None = Field(None, alias="Movement Count")
    disc_count: int | None = Field(None, alias="Disc Count")
    track_count: int | None = Field(None, alias="Track Count")
    artwork_count: int | None = Field(None, alias="Artwork Count")

    file_folder_count: int | None = Field(None, alias="File Folder Count")
    library_folder_count: int | None = Field(None, alias="Library Folder Count")
    protected: bool = Field(False, alias="Protected")
    music_video: bool = Field(False, alias="Music Video")
    has_video: bool = Field(False, alias="Has Video")
    part_of_gapless_album: bool | None = Field(None, alias="Part Of Gapless Album")
    playlist_only: bool = Field(False, alias="Playlist Only")
    """Apple Music tracks, that are a part of a playlist but not in the library"""
    purchased: bool = Field(False, alias="Purchased")
    """True for kind=Купленное аудио AAC"""
    matched: bool = Field(False, alias="Matched")
    explicit: bool = Field(False, alias="Explicit")
    clean: bool = Field(False, alias="Clean")
    hd: bool = Field(False, alias="HD")
    """For video files"""

    @property
    def path_artist(self):
        if self.compilation:
            return "Compilations"
        elif self.album_artist:
            return self.album_artist
        elif self.artist:
            return self.artist
        else:
            return "Unknown Artist"

    @property
    def path_album(self):
        return self.album or "Unknown Album"

    def possible_locations(self, max_suffix: int = 1) -> list[Path]:
        """
        Try different extensions since we can't rely on XML data to guess which extension to expect.
        Try MP3 first, with 40 chars name limit first (old convention that contains mostly original MP3s).
        """
        # duplicate track suffix, 0 means no suffix
        suffixes = list(range(max_suffix + 1))

        # filename length limit, 35 for newer iTunes/AM version, 40 for older
        name_limits = (40, 35)  # try old first, they mostly contain mp3

        # file extension
        extensions = ("mp3", "m4a")

        # whether to include a disc number in the filename
        disc_nums: list[int | None] = [None]
        if self.disc_number is not None:
            disc_nums.append(self.disc_number)

        paths = []
        # combine them into the cartesian product
        for sfx, lim, ext, disc_n in itertools.product(suffixes, name_limits, extensions, disc_nums):
            path = generate_path(
                artist=self.path_artist,
                album=self.path_album,
                title=self.name,
                ext=ext,
                track_n=self.track_number,
                disc_n=disc_n,
                suffix=sfx,
                name_limit=lim,
            )
            paths.append(path)

        # deduplicate the list, preserving the order
        return list(dict.fromkeys(paths))
