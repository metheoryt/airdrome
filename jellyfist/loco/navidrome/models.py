from datetime import datetime

from sqlmodel import SQLModel, create_engine, Field, Relationship
from sqlalchemy.orm import registry
from jellyfist.conf import settings
import string
import random


class NVSQLModel(SQLModel, registry=registry()):
    pass


engine = create_engine(settings.navidrome_db_dsn, echo=False)


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
    playlist_id: str = Field(foreign_key="playlist.id")
    media_file_id: str = Field(foreign_key="media_file.id")


class MediaFile(NVSQLModel, table=True):
    __tablename__ = "media_file"
    id: str = Field(primary_key=True, default_factory=generate_id)
    path: str
    title: str
    artist: str
    album: str
    playlists: list["Playlist"] = Relationship(back_populates="media_files", link_model=PlaylistTracks)


class Playlist(NVSQLModel, table=True):
    __tablename__ = "playlist"

    id: str = Field(primary_key=True, default_factory=generate_id)
    name: str = Field("")
    comment: str = Field("")
    duration: float = Field(0)
    song_count: int = Field(0)
    public: bool = Field(False)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    path: str = Field("")
    sync: bool = Field(False)
    size: int = Field(0)
    rules: str | None = Field(None)
    evaluated_at: datetime | None = Field(None)
    owner_id: str
    media_files: list["MediaFile"] = Relationship(back_populates="playlists", link_model=PlaylistTracks)
