from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from airdrome.enums import Source
from airdrome.models import Track, TrackFile, TrackPlay
from airdrome.navidrome.models import Album, Annotation, MediaFile, NavidromeBase, User
from airdrome.navidrome.sync.tracks import TrackSyncer


NV_PATH = "Music/Test Artist/Test Album/song.mp3"
LIB_PATH = f"Library/{NV_PATH}"


@pytest.fixture()
def nv_session():
    """In-memory SQLite session mirroring Navidrome's schema (one shared connection)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    NavidromeBase.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed_navidrome(nvs: Session, username: str = "me") -> User:
    """Create the NV-side user, album, and the one MediaFile organize would target."""
    user = User(user_name=username)
    album = Album(id="alb1", name="Test Album", created_at=datetime(2020, 1, 1, tzinfo=UTC))
    mf = MediaFile(
        id="mf1",
        path=NV_PATH,
        title="Song",
        artist="Test Artist",
        album="Test Album",
        album_id=album.id,
        birth_time=datetime(2020, 1, 1, tzinfo=UTC),
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        duration=180.0,
        size=1000,
    )
    nvs.add_all([user, album, mf])
    nvs.flush()
    return user


def _make_track(s: Session, title: str, **kw) -> Track:
    t = Track(title=title, artist="Test Artist", album="Test Album", **kw)
    s.add(t)
    s.flush()
    return t


def _add_plays(s: Session, track: Track, n: int, start_day: int = 1) -> None:
    """Attach `n` distinct TrackPlay rows to `track`."""
    for i in range(n):
        s.add(
            TrackPlay(
                track_id=track.id,
                played_at=datetime(2021, 1, start_day + i, 12, 0, tzinfo=UTC),
                platform=Source.LASTFM,
            )
        )
    s.flush()


def _annotation(nvs: Session, user: User) -> Annotation | None:
    return nvs.scalars(
        select(Annotation).where(
            Annotation.item_id == "mf1",
            Annotation.item_type == Annotation.ItemType.MEDIA_FILE,
            Annotation.user_id == user.id,
        )
    ).one_or_none()


def test_sync_single_track_pushes_its_plays(session, nv_session):
    """A plain track (no dedup) pushes exactly its own play count."""
    user = _seed_navidrome(nv_session)
    track = _make_track(session, "Song")
    session.add(
        TrackFile(source_path="/src/song.mp3", track_id=track.id, is_main=True, library_path=LIB_PATH)
    )
    session.flush()
    _add_plays(session, track, 3)

    TrackSyncer("me").sync_all(session, nv_session)

    ann = _annotation(nv_session, user)
    assert ann is not None
    assert ann.play_count == 3


def test_sync_aggregates_plays_across_dedup_group(session, nv_session):
    """Plays scattered over canon + twin both reach the group's one main file.

    The main file is owned by the *twin* here, the case that previously dropped
    the canon's plays entirely.
    """
    user = _seed_navidrome(nv_session)
    canon = _make_track(session, "Song")
    twin = _make_track(session, "Song (dup)", canon_id=canon.id)

    # The twin owns the organized main file; the canon owns none.
    session.add(TrackFile(source_path="/src/twin.mp3", track_id=twin.id, is_main=True, library_path=LIB_PATH))
    session.flush()

    _add_plays(session, canon, 5, start_day=1)
    _add_plays(session, twin, 2, start_day=20)

    TrackSyncer("me").sync_all(session, nv_session)

    ann = _annotation(nv_session, user)
    assert ann is not None
    assert ann.play_count == 7  # 5 (canon) + 2 (twin), not just the main owner's 2


def test_sync_rating_and_loved_taken_from_whole_group(session, nv_session):
    """rating = max over the group; starred = any member loved."""
    user = _seed_navidrome(nv_session)
    canon = _make_track(session, "Song", rating=2, loved=False)
    twin = _make_track(session, "Song (dup)", canon_id=canon.id, rating=5, loved=True)

    session.add(TrackFile(source_path="/src/c.mp3", track_id=canon.id, is_main=True, library_path=LIB_PATH))
    session.flush()
    _ = twin

    TrackSyncer("me").sync_all(session, nv_session)

    ann = _annotation(nv_session, user)
    assert ann is not None
    assert ann.rating == 5  # highest in the group, set on the twin
    assert ann.starred is True  # twin is loved
