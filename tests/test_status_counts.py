from sqlalchemy import text

from airdrome.enums import DecisionKind, Verdict
from airdrome.models import Track, TrackAlias, TrackFile, WantedVerdict
from airdrome.terminal.status import pipeline_counts
from tests.factories import add_canon, add_pair, make_track


def test_pipeline_counts_shape(session):
    """Counts come back as data: sections of named integers."""
    t1 = make_track(session, title="one")
    make_track(session, title="two")
    session.add(TrackFile(source_path="/x/a.mp3", track=t1))
    session.add(TrackAlias(title="one", artist="a", album="b", track_id=t1.id))
    session.add(TrackAlias(title="two", artist="a", album="b"))
    session.flush()

    counts = pipeline_counts(session)

    assert counts["canonical"]["tracks"] == 2
    assert counts["canonical"]["aliases"] == 2
    assert counts["canonical"]["aliases_matched"] == 1
    assert counts["files"]["files"] == 1
    assert counts["files"]["bound"] == 1
    assert counts["synced"]["playlist_links"] == 0
    assert counts["wanted"] == {"verdict_wanted": 0, "verdict_skip": 0}


def test_pipeline_counts_wanted_verdicts(session):
    """The wanted section tallies verdict rows by kind."""
    t1 = make_track(session, title="one")
    t2 = make_track(session, title="two")
    session.add(WantedVerdict(track_id=t1.id, verdict=Verdict.WANTED))
    session.add(WantedVerdict(track_id=t2.id, verdict=Verdict.SKIP))
    session.flush()

    assert pipeline_counts(session)["wanted"] == {"verdict_wanted": 1, "verdict_skip": 1}


def test_status_counts_decisions(session):
    """same/different count non-dead rows by kind (superseded included); dead and canons apart."""
    a, b, c, d = (make_track(session, title=n) for n in "abcd")
    # Oldest first; the resolver applies newest first, so the oldest `same` meets a separation and loses.
    add_pair(session, a, c, DecisionKind.SAME)  # superseded by a|b + b~c
    add_pair(session, b, c, DecisionKind.SAME)
    add_pair(session, a, b, DecisionKind.DIFFERENT)
    ghost = Track(title="gone")  # never flushed: its key names no live track
    add_pair(session, d, ghost, DecisionKind.SAME)  # dead pair; its kind (same) must not be counted
    add_canon(session, a)

    counts = pipeline_counts(session)["dedup"]

    assert (counts["same"], counts["different"]) == (2, 1)
    assert (counts["superseded"], counts["dead"], counts["canons"]) == (1, 1, 1)


def test_status_counts_decisions_zero_without_tables(session):
    """A DB with no decision tables reports zeros instead of crashing."""
    session.execute(text("DROP TABLE pairdecision, canonchoice"))
    counts = pipeline_counts(session)["dedup"]
    assert [counts[k] for k in ("same", "different", "canons", "superseded", "dead")] == [0] * 5
