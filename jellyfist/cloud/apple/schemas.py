import itertools
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jellyfist.cloud.apple.utils import ensure_truncated
from jellyfist.enums import Kind
from jellyfist.normalize.norm import normalize_name


class PlaylistTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: int = Field(alias="Track ID")


class PlaylistSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(alias="Name")
    description: str = Field(alias="Description")
    playlist_id: int = Field(alias="Playlist ID")
    persistent_id: str = Field(alias="Playlist Persistent ID")
    parent_persistent_id: str | None = Field(None, alias="Parent Persistent ID")
    all_items: bool = Field(alias="All Items")

    master: bool = Field(False, alias="Master")
    visible: bool = Field(True, alias="Visible")
    music: bool = Field(False, alias="Music")
    folder: bool = Field(False, alias="Folder")
    smart_info: bytes | None = Field(None, alias="Smart Info")
    smart_criteria: bytes | None = Field(None, alias="Smart Criteria")
    distinguished_kind: int | None = Field(None, alias="Distinguished Kind")
    favorited: bool = Field(False, alias="Favorited")
    loved: bool = Field(False, alias="Loved")

    items: list[PlaylistTrack] = Field(alias="Playlist Items")


class TrackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # database cols
    id: int | None = Field(None)
    path: str | None = Field(None)

    track_id: int = Field(alias="Track ID")
    track_type: Literal["URL", "Remote", "File"] = Field(alias="Track Type")
    name: str = Field(alias="Name")
    name_norm: str  # auto
    persistent_id: str = Field(alias="Persistent ID")
    size: int = Field(alias="Size")

    apple_music: bool = Field(False, alias="Apple Music")  # 6558
    """True means the track was added from Apple Music, and not from the local library"""

    kind: Kind | None = Field(None, alias="Kind")
    total_time: int | None = Field(None, alias="Total Time")
    track_number: int | None = Field(None, alias="Track Number")
    year: int | None = Field(None, alias="Year")
    date_modified: datetime | None = Field(None, alias="Date Modified")
    date_added: datetime | None = Field(None, alias="Date Added")
    rating: int | None = Field(None, alias="Rating")
    rating_computed: bool = Field(False, alias="Rating Computed")
    album_rating: int | None = Field(None, alias="Album Rating")
    album_rating_computed: bool = Field(False, alias="Album Rating Computed")
    music_video: bool = Field(False, alias="Music Video")
    has_video: bool = Field(False, alias="Has Video")
    file_folder_count: int | None = Field(None, alias="File Folder Count")
    library_folder_count: int | None = Field(None, alias="Library Folder Count")
    artist: str | None = Field(None, alias="Artist")
    """Unknown Artist in Explorer"""
    artist_norm: str = Field("")  # auto
    album_artist: str | None = Field(None, alias="Album Artist")
    album_artist_norm: str = Field("")
    album: str | None = Field(None, alias="Album")
    """Unknown Album in Explorer"""
    album_norm: str = Field("")  # auto
    grouping: str | None = Field(None, alias="Grouping")
    """An old field I've used to mark full albums"""
    genre: str | None = Field(None, alias="Genre")
    location: str | None = Field(None, alias="Location")
    """MacOS path (since we only can export the XML from Mac now)"""

    protected: bool = Field(False, alias="Protected")

    # extra fields
    release_date: datetime | None = Field(None, alias="Release Date")
    comments: str | None = Field(None, alias="Comments")
    sort_name: str | None = Field(None, alias="Sort Name")
    disc_number: int | None = Field(None, alias="Disc Number")
    play_count: int | None = Field(None, alias="Play Count")
    play_date: datetime | None = Field(None, alias="Play Date")
    play_date_utc: datetime | None = Field(None, alias="Play Date UTC")
    sort_composer: str | None = Field(None, alias="Sort Composer")
    sort_artist: str | None = Field(None, alias="Sort Artist")
    sort_album_artist: str | None = Field(None, alias="Sort Album Artist")
    sort_album: str | None = Field(None, alias="Sort Album")
    album_loved: bool = Field(False, alias="Album Loved")
    disc_count: int | None = Field(None, alias="Disc Count")
    track_count: int | None = Field(None, alias="Track Count")
    sample_rate: int | None = Field(None, alias="Sample Rate")
    skip_count: int | None = Field(None, alias="Skip Count")
    skip_date: datetime | None = Field(None, alias="Skip Date")
    work: str | None = Field(None, alias="Work")
    composer: str | None = Field(None, alias="Composer")
    loved: bool = Field(False, alias="Loved")
    favorited: bool = Field(False, alias="Favorited")
    """Alias for `loved`"""
    part_of_gapless_album: bool | None = Field(None, alias="Part Of Gapless Album")
    purchased: bool = Field(False, alias="Purchased")
    """True for kind=Купленное аудио AAC"""
    matched: bool = Field(False, alias="Matched")
    compilation: bool | None = Field(None, alias="Compilation")
    """Has "Compilations" for Artist folder."""
    explicit: bool = Field(False, alias="Explicit")
    normalization: int | None = Field(None, alias="Normalization")
    hd: bool = Field(False, alias="HD")
    """For video files"""
    volume_adjustment: int | None = Field(None, alias="Volume Adjustment")
    movement_name: str | None = Field(None, alias="Movement Name")
    movement_count: int | None = Field(None, alias="Movement Count")
    disliked: bool = Field(False, alias="Disliked")

    # not goes into a database
    playlist_only: bool = Field(False, alias="Playlist Only")  # False for all
    """Apple Music tracks, that are a part of a playlist but not in the library"""
    artwork_count: int | None = Field(None, alias="Artwork Count")  # None for 82, 1 for others
    bit_rate: int | None = Field(None, alias="Bit Rate")  # can be calculated
    bpm: int | None = Field(None, alias="BPM")  # cab be calculated
    clean: bool = Field(False, alias="Clean")  # 32 True

    @model_validator(mode="before")
    @classmethod
    def set_normalized_data(cls, data):
        for f, nf in (
            ("Name", "name_norm"),
            ("Artist", "artist_norm"),
            ("Album", "album_norm"),
            ("Album Artist", "album_artist_norm"),
        ):
            if f in data:
                data[nf] = normalize_name(data[f])
        return data

    @property
    def short_info(self):
        tn = self.track_number if self.track_number is not None else "-"
        cloud = "cloud" if self.apple_music else "local"
        added = self.date_added.strftime("%Y-%m-%d %H:%M:%S")
        return f"[{self.track_id}] {self.artist} ({self.album}) {tn}. {self.name} added {added} [{cloud}]"

    @property
    def path_artist(self):
        if self.compilation:
            artist = "Compilations"
        elif self.album_artist:
            artist = self.album_artist
        elif self.artist:
            artist = self.artist
        else:
            artist = "Unknown Artist"
        return artist

    @property
    def path_album(self):
        return self.album or "Unknown Album"

    def get_filename(self, with_disc_num: bool) -> str:
        """Return filename, without extension and suffix, not quoted or truncated."""
        name = self.name
        if self.track_number is not None:
            tn = f"{self.track_number:02}"
            if self.disc_number and with_disc_num:
                tn = f"{self.disc_number}-{tn}"
            name = f"{tn} {name}"
        return name

    def get_filename_safe(
        self,
        ext: str,
        with_disc_num: bool = False,
        name_limit: int = 35,
        suffix: str = "",
    ):
        filename = self.get_filename(with_disc_num=with_disc_num)
        suffix_ext = f"{suffix}.{ext}"
        name_maxlen = name_limit - len(suffix_ext)
        filename = ensure_truncated(filename, maxlen=name_maxlen, is_filename=True)
        return f"{filename}{suffix_ext}"

    def generate_location(
        self,
        ext: str,
        include_disc_num: bool = False,
        name_limit: int = 35,
        suffix: str = "",
    ) -> Path:
        artist = ensure_truncated(self.path_artist, maxlen=name_limit)
        album = ensure_truncated(self.path_album, maxlen=name_limit)
        filename = self.get_filename_safe(
            ext=ext,
            with_disc_num=include_disc_num,
            name_limit=name_limit,
            suffix=suffix,
        )
        return Path(artist) / Path(album) / Path(filename)

    def possible_locations(self, max_suffix: int = 1) -> Iterator[Path]:
        """
        Try different extensions, since we can't rely on XML data to guess which extension to expect.
        Try MP3 first, with 40 chars name limit first (old structure that contains mostly original MP3s).
        Also try to put trailing " 1" in the name, so we can extract more copies.
        """
        # duplicate track suffix, 0 means no suffix
        suffixes = (f" {v}" if v > 0 else "" for v in range(max_suffix + 1))
        # filename length limit, 35 for newer iTunes/AM version, 40 for older
        name_limits = (40, 35)  # try old first, they mostly contain mp3
        # file extension
        extensions = ("mp3", "m4a")
        # whether to include a disc number into the filename
        with_disc_nums = (False, True)

        # combine them into the cartesian product
        for sfx, lim, ext, with_disc_num in itertools.product(
            suffixes, name_limits, extensions, with_disc_nums
        ):
            yield self.generate_location(
                ext=ext,
                include_disc_num=with_disc_num,
                name_limit=lim,
                suffix=sfx,
            )
