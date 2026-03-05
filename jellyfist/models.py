from datetime import datetime, timezone
from pathlib import Path
from typing import Type, TypeVar, Any, Annotated, TYPE_CHECKING, Optional

import sqlalchemy as sa
from pydantic import model_validator, ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine, Relationship, Session, select

from .conf import settings
from .enums import Platform
from .normalize.norm import normalize_name
from .cloud.apple.utils import generate_path


if TYPE_CHECKING:
    from jellyfist.cloud.apple.models import AppleTrack


T = TypeVar("T", bound="Base")


AwareDatetime = Annotated[datetime, Field(sa_column=sa.Column(sa.DateTime(timezone=True)))]

AwareDatetimeDefNow = Annotated[
    datetime,
    Field(
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(timezone.utc),
    ),
]


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
        instance = cls(**params)
        session.add(instance)
        session.flush([instance])
        return instance, True


class TrackFile(Base, table=True):
    __table_args__ = (UniqueConstraint("track_id", "path"),)
    id: int | None = Field(default=None, primary_key=True)

    track_id: int = Field(foreign_key="track.id", index=True, ondelete="CASCADE")
    track: Track = Relationship(back_populates="files")
    path: Path = Field(sa_column=sa.Column(sa.String, nullable=False))


class Track(Base, table=True):
    """A representation of a single track in a library."""

    __table_args__ = (UniqueConstraint("title", "artist", "album", "album_artist"),)

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

    main_path: Path | None = Field(None, sa_column=sa.Column(sa.String, nullable=True))

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

    def generate_main_path(self, ext: str) -> Path:
        """Keep the main path consistent with Apple Library paths."""
        return generate_path(
            artist=self.path_artist,
            album=self.path_album,
            title=self.title,
            ext=ext,
            track_n=self.track_n,
            disc_n=self.disc_n,
        )


class TrackAlias(Base, table=True):
    __table_args__ = (UniqueConstraint("title", "album", "artist"),)

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
    date: AwareDatetime = Field(unique=True)
    platform: Platform


engine = create_engine(str(settings.db_dsn), echo=settings.db_echo)
