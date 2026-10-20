import pytest
from sqlalchemy.exc import IntegrityError

from airdrome.enums import Verdict
from airdrome.models import Track, TrackAlias, WantedVerdict


def test_verdict_requires_exactly_one_target(session):
    """A verdict row names a track or an alias, never both and never neither."""
    track = Track(title="T", artist="A", album="B")
    alias = TrackAlias(title="T", artist="A", album="B")
    session.add_all([track, alias])
    session.flush()

    session.add(WantedVerdict(track_id=track.id, alias_id=alias.id, verdict=Verdict.WANTED))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    session.add(WantedVerdict(verdict=Verdict.SKIP))
    with pytest.raises(IntegrityError):
        session.flush()
