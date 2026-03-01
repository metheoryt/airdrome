from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine, Relationship

from .conf import settings
from .enums import Platform, TrackType, Kind


class TrackPlaylistLink(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("track_id", "playlist_id", name="uq_trackplaylistlink_track_id_playlist_id"),
    )

    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="track.id", index=True, ondelete="CASCADE")
    playlist_id: int = Field(foreign_key="playlist.id", index=True, ondelete="CASCADE")
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
        sa.Index(
            "track_name_norm_trgm_idx",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={
                "name_norm": "gin_trgm_ops",
            },
        ),
        sa.Index(
            "track_artist_norm_trgm_idx",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={
                "artist_norm": "gin_trgm_ops",
            },
        ),
        sa.Index(
            "track_album_artist_norm_trgm_idx",
            "album_artist_norm",
            postgresql_using="gin",
            postgresql_ops={
                "album_artist_norm": "gin_trgm_ops",
            },
        ),
        sa.Index(
            "track_album_norm_trgm_idx",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={
                "album_norm": "gin_trgm_ops",
            },
        ),
        sa.Index("track_name_norm_artist_norm_album_norm_idx", "name_norm", "artist_norm", "album_norm"),
        sa.Index(
            "track_name_norm_album_artist_norm_album_norm_idx", "name_norm", "album_artist_norm", "album_norm"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    # files: list["TrackFile"] = Relationship(back_populates="track", cascade_delete=True)
    aliases: list["TrackAlias"] = Relationship(back_populates="track", cascade_delete=True)
    playlists: list["Playlist"] = Relationship(back_populates="tracks", link_model=TrackPlaylistLink)

    # Apple Music data
    name_norm: str
    name: str
    album_norm: str = Field("")
    album: str | None = Field(None)
    """Unknown Album in Explorer"""
    artist_norm: str = Field("")
    artist: str | None = Field(None)
    """Unknown Artist in Explorer"""
    album_artist_norm: str = Field("")
    album_artist: str | None = Field(None)

    track_id: int = Field(unique=True)
    track_type: TrackType = Field(sa_column=sa.Column(sa.Enum(TrackType, native_enum=False)))
    persistent_id: str = Field(unique=True)
    size: int

    apple_music: bool = Field(False)
    """True means the track was added from Apple Music, and not from the local library"""

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

    # jellyfist data
    path: Path | None = Field(None)  # main path of a track (already transferred)

    @property
    def repr(self):
        return f"[{self.name} / {self.artist or ''} / {self.album or ''}]"

    @property
    def artist_album_name(self):
        return f"{self.artist or ''}/{self.album or ''}/{self.name or ''}"

    @property
    def short_info(self):
        tn = str(self.track_number) if self.track_number is not None else "-"
        tn += "."
        cloud = "cloud" if self.apple_music else "local"
        added = self.date_added.strftime("%Y-%m-%d %H:%M:%S")
        tt = ""
        if self.total_time:
            secs = self.total_time // 1000
            tt = f"{secs // 60}:{secs % 60:02d}"
        return f"#{self.track_id:<6} [{cloud}] {self.artist or '':<40} {self.album or '':<40} {tn:<3} {self.name:<40} ({tt}) added {added}"


# class TrackFile(SQLModel, table=True):
#     id: int | None = Field(default=None, primary_key=True)
#
#     track_id: int = Field(foreign_key="track.id", index=True, ondelete="CASCADE")
#     track: Track = Relationship(back_populates="files")
#
#     path: str


class TrackAlias(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("title", "album", "artist", name="uq_trackalias_title_album_artist"),)
    id: int | None = Field(default=None, primary_key=True)

    artist: str | None = Field(None)
    title: str | None = Field(None)
    album: str | None = Field(None)
    artist_norm: str = Field("")
    title_norm: str = Field("")
    album_norm: str = Field("")

    track_id: int | None = Field(None, foreign_key="track.id", index=True)
    track: Track | None = Relationship(back_populates="aliases")

    scrobbles: list["TrackAliasScrobble"] = Relationship(back_populates="alias", cascade_delete=True)

    @property
    def repr(self):
        return f"[{self.title} / {self.artist or ''} / {self.album or ''}]"


class TrackAliasScrobble(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    alias_id: int = Field(foreign_key="trackalias.id", index=True, ondelete="CASCADE")
    alias: TrackAlias = Relationship(back_populates="scrobbles")
    date: datetime = Field(unique=True)
    platform: Platform


engine = create_engine(str(settings.db_dsn), echo=settings.db_echo)
