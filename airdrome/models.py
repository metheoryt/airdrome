from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional, Type, TypeVar

import sqlalchemy as sa
from mutagen import File
from pydantic import ConfigDict, model_validator
from sqlalchemy import UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, Index, Relationship, Session, SQLModel, create_engine, select, text

from .cloud.apple.utils import generate_path
from .conf import settings
from .enums import Platform
from .normalize.norm import normalize_name


if TYPE_CHECKING:
    from airdrome.cloud.apple.models import AppleTrack


T = TypeVar("T", bound="Base")


AwareDatetime = Annotated[datetime, Field(sa_column=sa.Column(sa.DateTime(timezone=True)))]

AwareDatetimeDefNow = Annotated[
    datetime,
    Field(
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(timezone.utc),
    ),
]


class PathType(TypeDecorator):
    impl = sa.String
    cache_ok = True

    def process_bind_param(self, value: Path | None, dialect):
        if value is None:
            return None
        return value.as_posix()

    def process_result_value(self, value: str, dialect):
        if value is None:
            return None
        return Path(value)


class Base(SQLModel):
    @classmethod
    def get_or_create(
        cls: Type[T], session: Session, defaults: dict[str, Any] | None = None, **lookups: Any
    ) -> tuple[T, bool]:
        statement = select(cls).filter_by(**lookups)
        instance = session.exec(statement).one_or_none()

        if instance:
            return instance, False

        params = {**lookups, **(defaults or {})}
        instance = cls.model_validate(params)  # to trigger model validation
        session.add(instance)
        session.flush([instance])
        return instance, True

    @classmethod
    def truncate_cascade(cls, session: Session):
        session.exec(text(f"TRUNCATE TABLE {cls.__tablename__} RESTART IDENTITY CASCADE;"))
        session.commit()


class Track(Base, table=True):
    """A representation of a single track in a library."""

    __table_args__ = (
        UniqueConstraint("title", "artist", "album", "album_artist"),
        # trigram indexes for matching
        Index(
            "ix_track_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_track_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_track_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
        Index(
            "ix_track_album_artist_norm_trgm",
            "album_artist_norm",
            postgresql_using="gin",
            postgresql_ops={"album_artist_norm": "gin_trgm_ops"},
            postgresql_where=text("album_artist_norm <> ''"),
        ),
    )

    model_config = ConfigDict(validate_assignment=True)  # rerun validation on field assignment

    id: int | None = Field(default=None, primary_key=True)

    title: str = Field()
    artist: str | None = Field(None)
    album_artist: str | None = Field(None)
    album: str | None = Field(None)

    track_n: int | None = Field(None)
    disc_n: int | None = Field(None)
    compilation: bool | None = Field(None)

    title_norm: str = Field("")
    artist_norm: str = Field("")
    album_artist_norm: str = Field("")
    album_norm: str = Field("")

    main_path: Path | None = Field(None, sa_column=sa.Column(PathType(), nullable=True))

    # duplicates
    canon_id: int | None = Field(None, foreign_key="track.id", index=True, ondelete="SET NULL")
    canon: Optional["Track"] = Relationship(
        back_populates="twins",
        sa_relationship_kwargs={"remote_side": "Track.id"},
    )
    twins: list["Track"] = Relationship(back_populates="canon")

    apple_tracks: list["AppleTrack"] = Relationship(back_populates="track", cascade_delete=True)
    aliases: list["TrackAlias"] = Relationship(back_populates="track", cascade_delete=True)
    files: list["TrackFile"] = Relationship(back_populates="track", cascade_delete=True)

    @property
    def table_row(self) -> tuple[str, str | None, str | None, str | None]:
        return self.title, self.artist, self.album_artist, self.album

    @model_validator(mode="before")
    @classmethod
    def _populate_normalized_fields(cls, data: Any):
        field_map = (
            ("title", "title_norm"),
            ("artist", "artist_norm"),
            ("album_artist", "album_artist_norm"),
            ("album", "album_norm"),
        )
        if isinstance(data, dict):
            # raw data
            for f, nf in field_map:
                if f in data:
                    data[nf] = normalize_name(data[f])
        elif isinstance(data, cls):
            # existing SQLModel instance
            for f, nf in field_map:
                val = getattr(data, f, None)
                setattr(data, nf, normalize_name(val))
        else:
            raise ValueError("Unexpected type:", type(data))
        return data

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

    def generate_main_path(self, ext: str, suffix: int = 0) -> Path:
        """Keep the main path consistent with Apple Library paths."""
        return generate_path(
            artist=self.path_artist,
            album=self.path_album,
            title=self.title,
            ext=ext,
            track_n=self.track_n,
            disc_n=self.disc_n,
            suffix=suffix,
        )


class TrackFile(Base, table=True):
    __table_args__ = (
        # trigram indexes for matching
        Index(
            "ix_trackfile_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_trackfile_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_trackfile_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
        Index(
            "ix_trackfile_album_artist_norm_trgm",
            "album_artist_norm",
            postgresql_using="gin",
            postgresql_ops={"album_artist_norm": "gin_trgm_ops"},
            postgresql_where=text("album_artist_norm <> ''"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    path: Path = Field(sa_column=sa.Column(PathType(), nullable=False, unique=True))

    track_id: int | None = Field(foreign_key="track.id", index=True, ondelete="CASCADE")
    track: Track | None = Relationship(back_populates="files")

    duration: float | None = Field(None)
    bitrate: int | None = Field(None)
    date: str | None = Field(None)

    title: str | None = Field(None)
    artist: str | None = Field(None)
    album_artist: str | None = Field(None)
    album: str | None = Field(None)

    title_norm: str = Field("")
    artist_norm: str = Field("")
    album_artist_norm: str = Field("")
    album_norm: str = Field("")

    def enrich(self):
        audio = File(self.path)
        if audio is None:
            raise ValueError("Unsupported or corrupted file")
        tags = audio.tags or {}

        def get(*keys):
            for k in keys:
                if k in tags:
                    v = tags[k]
                    return "; ".join(v) if isinstance(v, list) else str(v)
            return None

        self.artist = get("TPE1", "©ART")
        self.artist_norm = normalize_name(self.artist)

        self.album = get("TALB", "©alb")
        self.album_norm = normalize_name(self.album)

        self.album_artist = get("TPE2", "aART")
        self.album_artist_norm = normalize_name(self.album_artist)

        self.title = get("TIT2", "©nam")
        self.title_norm = normalize_name(self.title)

        self.date = get("TDRC", "TDOR", "©day")
        self.duration = getattr(audio.info, "length")
        self.bitrate = getattr(audio.info, "bitrate", 0)


class TrackAlias(Base, table=True):
    __table_args__ = (
        UniqueConstraint("title", "album", "artist"),
        # trigram indexes for matching
        Index(
            "ix_trackalias_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_trackalias_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_trackalias_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
    )

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

    @model_validator(mode="before")
    @classmethod
    def _populate_normalized_fields(cls, data: Any):
        field_map = (
            ("title", "title_norm"),
            ("artist", "artist_norm"),
            ("album", "album_norm"),
        )
        if isinstance(data, dict):
            # raw data
            for f, nf in field_map:
                if f in data:
                    data[nf] = normalize_name(data[f])
        elif isinstance(data, cls):
            # existing SQLModel instance
            for f, nf in field_map:
                val = getattr(data, f, None)
                setattr(data, nf, normalize_name(val))
        else:
            raise ValueError("Unexpected type:", type(data))
        return data

    @property
    def repr(self):
        return f"[{self.title} / {self.artist or ''} / {self.album or ''}]"


class TrackAliasScrobble(Base, table=True):
    id: int | None = Field(default=None, primary_key=True)
    alias_id: int = Field(foreign_key="trackalias.id", index=True, ondelete="CASCADE")
    alias: TrackAlias = Relationship(back_populates="scrobbles")
    date: datetime = Field(sa_column=sa.Column(sa.DateTime(timezone=True), unique=True))
    platform: Platform


engine = create_engine(str(settings.db_dsn), echo=settings.db_echo)
