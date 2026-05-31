from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.models import Track, TrackAlias, TrackFile

from .norm import normalize_name


def _renormalize(session: Session, model, fields: list[tuple[str, str]], label: str):
    for i, obj in enumerate(session.scalars(select(model)), start=1):
        for src, dst in fields:
            setattr(obj, dst, normalize_name(getattr(obj, src)))
        if i % 1000 == 0:
            session.flush()
    session.flush()
    console.print(f"[green]{label} normalized[/green]")


def normalize_track_names(s: Session):
    _renormalize(
        s,
        Track,
        [
            ("title", "title_norm"),
            ("artist", "artist_norm"),
            ("album_artist", "album_artist_norm"),
            ("album", "album_norm"),
        ],
        "track names",
    )


def normalize_alias_names(s: Session):
    _renormalize(
        s,
        TrackAlias,
        [
            ("title", "title_norm"),
            ("artist", "artist_norm"),
            ("album", "album_norm"),
        ],
        "alias names",
    )


def normalize_track_file_names(s: Session):
    _renormalize(
        s,
        TrackFile,
        [
            ("title", "title_norm"),
            ("artist", "artist_norm"),
            ("album_artist", "album_artist_norm"),
            ("album", "album_norm"),
        ],
        "track file names",
    )
