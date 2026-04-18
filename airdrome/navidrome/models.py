import random
import string
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import registry
from sqlmodel import Field, Relationship, SQLModel, create_engine

from airdrome.conf import settings


class NVSQLModel(SQLModel, registry=registry()):
    pass


_engine = None


def get_nv_engine():
    global _engine
    if _engine is None:
        if not settings.navidrome_db_dsn:
            raise RuntimeError("NAVIDROME_DB_DSN is not configured in .env")
        _engine = create_engine(settings.navidrome_db_dsn, echo=False)
    return _engine


def checkpoint_wal():
    """Fold any pending WAL pages into the main DB file before writing.

    Must be called after confirming Navidrome is stopped.
    """
    with get_nv_engine().connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


# these are existing Navidrome tables, only declare columns that are needed


def generate_id():
    return "".join(random.choices(string.ascii_letters + string.digits, k=22))


class User(NVSQLModel, table=True):
    __tablename__ = "user"
    id: str = Field(primary_key=True, default_factory=generate_id)
    user_name: str = Field("")


class PlaylistTracks(NVSQLModel, table=True):
    __tablename__ = "playlist_tracks"
    id: int | None = Field(None, primary_key=True)
    playlist_id: str = Field(foreign_key="playlist.id", primary_key=True)
    media_file_id: str = Field(foreign_key="media_file.id")


class Playlist(NVSQLModel, table=True):
    __tablename__ = "playlist"

    id: str = Field(primary_key=True, default_factory=generate_id)
    name: str = Field("")
    comment: str = Field("")
    duration: float = Field(0)
    song_count: int = Field(0)
    public: bool = Field(False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    path: str = Field("")
    sync: bool = Field(False)
    size: int = Field(0)
    rules: str | None = Field(None)
    evaluated_at: datetime | None = Field(None)
    owner_id: str
    media_files: list["MediaFile"] = Relationship(back_populates="playlists", link_model=PlaylistTracks)


class MediaFile(NVSQLModel, table=True):
    __tablename__ = "media_file"
    id: str = Field(primary_key=True, default_factory=generate_id)
    path: str
    title: str
    artist: str
    album: str
    album_id: str = Field(foreign_key="album.id")
    birth_time: datetime
    created_at: datetime
    duration: float
    size: int

    album_model: "Album" = Relationship(back_populates="media_files")
    playlists: list["Playlist"] = Relationship(back_populates="media_files", link_model=PlaylistTracks)


class Album(NVSQLModel, table=True):
    __tablename__ = "album"
    id: str = Field(primary_key=True, default_factory=generate_id)
    name: str
    created_at: datetime

    media_files: list["MediaFile"] = Relationship(back_populates="album_model")


class AlbumArtist(NVSQLModel, table=True):
    __tablename__ = "album_artists"
    album_id: str = Field(foreign_key="album.id", primary_key=True)
    artist_id: str = Field(foreign_key="artist.id", primary_key=True)
    role: str = Field("", primary_key=True)
    sub_role: str = Field("", primary_key=True)


class Annotation(NVSQLModel, table=True):
    """Annotates an Artist, Album, or Media File with rating, play count, and starred status."""

    __tablename__ = "annotation"

    class ItemType(StrEnum):
        MEDIA_FILE = "media_file"
        ALBUM = "album"
        ARTIST = "artist"

    user_id: str = Field(primary_key=True, foreign_key="user.id")
    item_id: str = Field(primary_key=True)  # can be a media file, album, or artist
    item_type: str = Field(primary_key=True)

    play_count: int = Field(0)
    play_date: datetime | None = Field(None)
    rating: int = Field(0)
    starred: bool = Field(False)
    starred_at: datetime | None = Field(None)
    rated_at: datetime | None = Field(None)


class Scrobbles(NVSQLModel, table=True):
    __tablename__ = "scrobbles"
    media_file_id: str = Field(foreign_key="media_file.id", primary_key=True)
    user_id: str = Field(foreign_key="user.id", primary_key=True)
    # UTC timestamp of a submission
    submission_time: int = Field(index=True, primary_key=True)
