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


def test_returns_counts(session):
    _make_track(session, "Song A", "Artist A")
    _make_track(session, "Song B", "Artist B")
    _make_alias(session, "Song A", "Artist A")
    _make_alias(session, "Song B", "Artist B")
    _make_alias(session, "Unknown Song", "Unknown Artist")

    matched, unmatched = do_match_aliases(session)

    assert matched == 2
    assert unmatched == 1


def test_suffix_containment_matches(session):
    # The catalog title carries a suffix the scrobble source dropped. Trigram similarity
    # scores this pair low, but substring containment + strong artist recovers it.
    track = _make_track(session, "None Shall Pass Radio Edit", "Aesop Rock")
    alias = _make_alias(session, "None Shall Pass", "Aesop Rock")

    matched, _ = do_match_aliases(session)

    assert matched == 1
    assert alias.track_id == track.id


def test_punctuation_only_difference_matches(session):
    # A surviving trailing "!" makes the normalized titles non-identical; containment recovers it.
    track = _make_track(session, "Clarity", "Zedd")
    alias = _make_alias(session, "Clarity!", "Zedd")

    matched, _ = do_match_aliases(session)

    assert matched == 1
    assert alias.track_id == track.id


def test_title_typo_with_strong_artist_matches(session):
    # One-character title typo: fuzzy title gate + exact artist clears the threshold.
    track = _make_track(session, "Meditate", "EarthGang")
    alias = _make_alias(session, "Mediate", "EarthGang")

    matched, _ = do_match_aliases(session)

    assert matched == 1
    assert alias.track_id == track.id


def test_artistless_alias_requires_exact_title(session):
    # With no artist to corroborate, a fuzzy/contained title must NOT match — otherwise
    # generic similar titles get mis-attached ("crickets in the rain" -> "caught in the rain").
    _make_track(session, "Caught in the Rain", "Revis")
    alias = _make_alias(session, "Crickets in the Rain", "")

    matched, unmatched = do_match_aliases(session)

    assert matched == 0
    assert unmatched == 1
    assert alias.track_id is None


def test_same_title_different_artist_not_matched(session):
    # Two distinct songs share a title. The relaxed title gate must NOT match them — the
    # artist weight has to keep the score below threshold.
    _make_track(session, "Inglorious", "Slowthai")
    alias = _make_alias(session, "Inglorious", "Tyler, The Creator")

    matched, unmatched = do_match_aliases(session)

    assert matched == 0
    assert unmatched == 1
    assert alias.track_id is None
