from datetime import UTC, datetime
from pathlib import Path

import pytest

from airdrome.enums import BindMethod, Source
from airdrome.match import find_best_match, find_best_track
from airdrome.models import Track, TrackAlias, TrackAliasScrobble, TrackFile
from airdrome.scrobbles.match_aliases import alias_ids, do_match_aliases


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


# --- the alias ladder: submitted id -> exact tags -> fuzzy; a mapped id only confirms ---

MBID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _tag_recording(s, track: Track, mbid: str) -> None:
    """Give ``track`` a file carrying ``mbid`` so the library knows that recording."""
    s.add(TrackFile(source_path=Path(f"/music/{track.id}.mp3"), track=track, recording_mbid=mbid))
    s.flush()


def _scrobble(s, alias: TrackAlias, minute: int, submitted: str | None = None, mapped: str | None = None):
    s.add(
        TrackAliasScrobble(
            alias=alias,
            date=datetime(2026, 1, 1, 12, minute, tzinfo=UTC),
            platform=Source.LISTENBRAINZ,
            submitted_mbid=submitted,
            mapped_mbid=mapped,
        )
    )
    s.flush()


def test_submitted_id_binds(session):
    # The tags disagree completely; only the submitted id can connect these.
    track = _make_track(session, "Totally Different Title", "Someone Else")
    _tag_recording(session, track, MBID)
    alias = _make_alias(session, "Bohemian Rhapsody", "Queen")
    _scrobble(session, alias, 0, submitted=MBID)

    matched, _ = do_match_aliases(session)

    assert matched == 1
    assert alias.track_id == track.id
    assert alias.match_method == BindMethod.MBID
    assert alias.match_confidence == 1.0


def test_submitted_id_binds_to_group_canon_of_lowest_track(session):
    canon = _make_track(session, "Song", "Artist", "Album A")
    twin = _make_track(session, "Song", "Artist", "Album B")
    twin.canon = canon
    session.flush()
    # The twin is the lowest-id carrier of the recording here? No: canon is lower, so make the
    # twin the only carrier and the canon a different, id-less track.
    _tag_recording(session, twin, MBID)
    alias = _make_alias(session, "whatever", "nobody")
    _scrobble(session, alias, 0, submitted=MBID)

    do_match_aliases(session)

    assert alias.track_id == canon.id


def test_unknown_submitted_id_falls_through_to_tags(session):
    track = _make_track(session, "Hotel California", "Eagles")
    alias = _make_alias(session, "Hotel California", "Eagles")
    _scrobble(session, alias, 0, submitted=OTHER)

    do_match_aliases(session)

    assert alias.track_id == track.id
    assert alias.match_method == BindMethod.TAGS_EXACT
    assert alias.match_confidence == 1.0


def test_fuzzy_match_records_score_as_confidence(session):
    track = _make_track(session, "None Shall Pass Radio Edit", "Aesop Rock")
    alias = _make_alias(session, "None Shall Pass", "Aesop Rock")

    do_match_aliases(session)

    assert alias.track_id == track.id
    assert alias.match_method == BindMethod.TAGS_FUZZY
    assert alias.match_confidence is not None
    assert 0.4 <= alias.match_confidence <= 1.0


def test_mapped_id_alone_does_not_bind(session):
    track = _make_track(session, "Unrelated Title", "Unrelated Artist")
    _tag_recording(session, track, MBID)
    alias = _make_alias(session, "Bohemian Rhapsody", "Queen")
    _scrobble(session, alias, 0, mapped=MBID)

    matched, unmatched = do_match_aliases(session)

    assert (matched, unmatched) == (0, 1)
    assert alias.track_id is None
    assert alias.match_method is None


def test_mapped_id_boosts_fuzzy_confidence_by_a_tenth(session):
    track = _make_track(session, "None Shall Pass Radio Edit", "Aesop Rock")
    _tag_recording(session, track, MBID)
    alias = _make_alias(session, "None Shall Pass", "Aesop Rock")
    _scrobble(session, alias, 0, mapped=MBID)
    found = find_best_match(session, alias.title_norm, alias.artist_norm, alias.album_norm)
    assert found is not None
    score = found[1]

    do_match_aliases(session)

    assert score < 0.9  # room to see the boost
    assert alias.match_method == BindMethod.TAGS_FUZZY
    assert alias.match_confidence == pytest.approx(score + 0.1)


def test_mapped_id_for_another_recording_does_not_boost(session):
    track = _make_track(session, "None Shall Pass Radio Edit", "Aesop Rock")
    _tag_recording(session, track, MBID)
    alias = _make_alias(session, "None Shall Pass", "Aesop Rock")
    _scrobble(session, alias, 0, mapped=OTHER)
    found = find_best_match(session, alias.title_norm, alias.artist_norm, alias.album_norm)
    assert found is not None
    score = found[1]

    do_match_aliases(session)

    assert alias.match_confidence == pytest.approx(score)


def test_mapped_id_breaks_tie(session):
    # Same text, different albums, alias names no album: the scores tie exactly.
    first = _make_track(session, "Same Song", "Same Artist", "Album One")
    second = _make_track(session, "Same Song", "Same Artist", "Album Two")
    _tag_recording(session, second, MBID)
    alias = _make_alias(session, "Same Song", "Same Artist")
    _scrobble(session, alias, 0, mapped=MBID)

    do_match_aliases(session)

    assert alias.match_method == BindMethod.TAGS_EXACT
    assert alias.track_id == second.id
    assert first.id != second.id


def test_mapped_id_breaks_tie_between_fuzzy_candidates(session):
    # Titles carry a suffix the alias lacks, so no exact rung; both candidates score the same.
    _make_track(session, "Same Song Radio Edit", "Same Artist", "Album One")
    second = _make_track(session, "Same Song Radio Edit", "Same Artist", "Album Two")
    _tag_recording(session, second, MBID)
    alias = _make_alias(session, "Same Song", "Same Artist")
    _scrobble(session, alias, 0, mapped=MBID)

    do_match_aliases(session)

    assert alias.match_method == BindMethod.TAGS_FUZZY
    assert alias.track_id == second.id


def test_without_mapped_id_the_tie_goes_to_the_default_order(session):
    first = _make_track(session, "Same Song Radio Edit", "Same Artist", "Album One")
    second = _make_track(session, "Same Song Radio Edit", "Same Artist", "Album Two")
    _tag_recording(session, second, MBID)

    default = find_best_track(session, "same song", "same artist", None)
    hinted = find_best_track(session, "same song", "same artist", None, prefer_mbid=MBID)

    assert default is not None
    assert hinted is not None
    assert default.id == first.id
    assert hinted.id == second.id


def test_disagreeing_listens_no_id(session):
    alias = _make_alias(session, "Song", "Artist")
    _scrobble(session, alias, 0, submitted=MBID, mapped=MBID)
    _scrobble(session, alias, 1, submitted=OTHER, mapped=OTHER)
    assert alias_ids(session, alias) == (None, None)

    # Listens without an id neither contribute nor veto.
    other = _make_alias(session, "Song 2", "Artist")
    _scrobble(session, other, 2, submitted=MBID)
    _scrobble(session, other, 3)
    assert alias_ids(session, other) == (MBID, None)
