from airdrome.models import Track, TrackAlias
from airdrome.scrobbles.match_aliases import do_match_aliases


def _make_track(s, title: str, artist: str = "", album: str = "") -> Track:
    t = Track(title=title, artist=artist, album=album)
    s.add(t)
    s.flush()
    return t


def _make_alias(s, title: str, artist: str = "", album: str = "") -> TrackAlias:
    a = TrackAlias(title=title, artist=artist, album=album)
    s.add(a)
    s.flush()
    return a


def test_exact_match(session):
    _make_track(session, "Bohemian Rhapsody", "Queen", "A Night at the Opera")
    alias = _make_alias(session, "Bohemian Rhapsody", "Queen", "A Night at the Opera")

    matched, unmatched = do_match_aliases(session)

    assert matched == 1
    assert unmatched == 0
    assert alias.track_id is not None


def test_no_match_below_threshold(session):
    _make_track(session, "Completely Different Song", "Other Artist", "Other Album")
    alias = _make_alias(session, "Bohemian Rhapsody", "Queen", "A Night at the Opera")

    matched, unmatched = do_match_aliases(session)

    assert matched == 0
    assert unmatched == 1
    assert alias.track_id is None


def test_already_matched_aliases_are_skipped(session):
    track = _make_track(session, "Hotel California", "Eagles")
    alias = _make_alias(session, "Hotel California", "Eagles")
    alias.track = track
    session.flush()

    matched, unmatched = do_match_aliases(session)

    # alias already has track_id, so it's excluded from the query
    assert matched == 0
    assert unmatched == 0


def test_reset_clears_existing_matches(session):
    track = _make_track(session, "Hotel California", "Eagles")
    alias = _make_alias(session, "Hotel California", "Eagles")
    alias.track = track
    session.flush()

    matched, unmatched = do_match_aliases(session, reset=True)

    # after reset, all aliases are unmatched first, then re-matched
    assert matched == 1
    assert unmatched == 0


def test_dry_run_does_not_commit(session):
    _make_track(session, "Stairway to Heaven", "Led Zeppelin")
    alias = _make_alias(session, "Stairway to Heaven", "Led Zeppelin")

    do_match_aliases(session, dry_run=True)

    # after rollback, alias should have no track_id
    session.refresh(alias)
    assert alias.track_id is None


def test_returns_counts(session):
    _make_track(session, "Song A", "Artist A")
    _make_track(session, "Song B", "Artist B")
    _make_alias(session, "Song A", "Artist A")
    _make_alias(session, "Song B", "Artist B")
    _make_alias(session, "Unknown Song", "Unknown Artist")

    matched, unmatched = do_match_aliases(session)

    assert matched == 2
    assert unmatched == 1
