import random
import string
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from airdrome.conf import settings


class NavidromeBase(DeclarativeBase):
    pass


class NVSQLModel(NavidromeBase):
    __abstract__ = True


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


class User(NVSQLModel):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(primary_key=True, default=generate_id)
    user_name: Mapped[str] = mapped_column(default="")


class PlaylistTracks(NVSQLModel):
    __tablename__ = "playlist_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[str] = mapped_column(ForeignKey("playlist.id"), primary_key=True)
    media_file_id: Mapped[str] = mapped_column(ForeignKey("media_file.id"))


class Playlist(NVSQLModel):
    __tablename__ = "playlist"

    id: Mapped[str] = mapped_column(primary_key=True, default=generate_id)
    name: Mapped[str] = mapped_column(default="")
    comment: Mapped[str] = mapped_column(default="")
    duration: Mapped[float] = mapped_column(default=0)
    song_count: Mapped[int] = mapped_column(default=0)
    public: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    path: Mapped[str] = mapped_column(default="")
    sync: Mapped[bool] = mapped_column(default=False)
    size: Mapped[int] = mapped_column(default=0)
    rules: Mapped[str | None]
    evaluated_at: Mapped[datetime | None]
    owner_id: Mapped[str]
    media_files: Mapped[list[MediaFile]] = relationship(
        back_populates="playlists", secondary="playlist_tracks"
    )


class MediaFile(NVSQLModel):
    __tablename__ = "media_file"
    id: Mapped[str] = mapped_column(primary_key=True, default=generate_id)
    path: Mapped[str]
    title: Mapped[str]
    artist: Mapped[str]
    album: Mapped[str]
    album_id: Mapped[str] = mapped_column(ForeignKey("album.id"))
    birth_time: Mapped[datetime]
    created_at: Mapped[datetime]
    duration: Mapped[float]
    size: Mapped[int]

    album_model: Mapped[Album] = relationship(back_populates="media_files")
    playlists: Mapped[list[Playlist]] = relationship(
        back_populates="media_files", secondary="playlist_tracks"
    )


class Album(NVSQLModel):
    __tablename__ = "album"
    id: Mapped[str] = mapped_column(primary_key=True, default=generate_id)
    name: Mapped[str]
    created_at: Mapped[datetime]

    media_files: Mapped[list[MediaFile]] = relationship(back_populates="album_model")


class AlbumArtist(NVSQLModel):
    __tablename__ = "album_artists"
    album_id: Mapped[str] = mapped_column(ForeignKey("album.id"), primary_key=True)
    artist_id: Mapped[str] = mapped_column(ForeignKey("artist.id"), primary_key=True)
    role: Mapped[str] = mapped_column(default="", primary_key=True)
    sub_role: Mapped[str] = mapped_column(default="", primary_key=True)


class Annotation(NVSQLModel):
    """Annotates an Artist, Album, or Media File with rating, play count, and starred status."""

    __tablename__ = "annotation"

    class ItemType(StrEnum):
        MEDIA_FILE = "media_file"
        ALBUM = "album"
        ARTIST = "artist"

    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(primary_key=True)  # can be a media file, album, or artist
    item_type: Mapped[str] = mapped_column(primary_key=True)

    play_count: Mapped[int] = mapped_column(default=0)
    play_date: Mapped[datetime | None]
    rating: Mapped[int] = mapped_column(default=0)
    starred: Mapped[bool] = mapped_column(default=False)
    starred_at: Mapped[datetime | None]
    rated_at: Mapped[datetime | None]


class Scrobbles(NVSQLModel):
    __tablename__ = "scrobbles"
    media_file_id: Mapped[str] = mapped_column(ForeignKey("media_file.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), primary_key=True)
    # UTC timestamp of a submission
    submission_time: Mapped[int] = mapped_column(index=True, primary_key=True)
