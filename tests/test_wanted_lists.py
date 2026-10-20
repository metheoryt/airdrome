"""Tests for the two `wanted` lists: library tracks with no file, and file-less plays by count."""

from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from airdrome.cloud.sources import SourceTrack
from airdrome.enums import BindMethod, Source, Verdict
from airdrome.models import (
    Playlist,
    PlaylistTrack,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
)
from airdrome.wanted.lists import unmatched_groups, wanted_library, wanted_scrobbled
from airdrome.wanted.verdicts import set_alias_verdict, set_track_verdict

from factories import make_track


_ids = count(1)
# `TrackAliasScrobble.date` is unique table-wide, so every scrobble gets its own instant.
_clock = count()


def _at(day: int) -> datetime:
    """A fixed tz-aware UTC date in January 2020."""
    return datetime(2020, 1, day, tzinfo=UTC)


def _source(s, track: Track, provider: Source) -> SourceTrack:
    """A flushed `SourceTrack` of `provider` bound to `track`."""
    st = SourceTrack(provider=provider, source_id=str(next(_ids)), track_id=track.id, title=track.title)
    s.add(st)
    s.flush()
    return st


def _file(s, track: Track) -> TrackFile:
    """A flushed `TrackFile` bound to `track`."""
    f = TrackFile(source_path=Path(f"/music/{next(_ids)}.mp3"), track_id=track.id, is_main=True)
    s.add(f)
    s.flush()
    return f


def _plays(s, track: Track, n: int, start: datetime | None = None) -> None:
    """Add `n` `TrackPlay`s on `track`, one hour apart from `start`."""
    start = start or _at(1)
    for i in range(n):
        s.add(TrackPlay(track_id=track.id, played_at=start + timedelta(hours=i), platform=Source.LASTFM))
    s.flush()


def _twin(s, twin: Track, canon: Track) -> None:
    """Link `twin` under `canon`."""
    twin.canon = canon
    twin.canon_method = BindMethod.DEDUP_FLAGS
    s.flush()


def _alias(s, title: str, artist: str | None = "x", album: str | None = "", scrobbles: int = 1) -> TrackAlias:
    """A flushed, unbound alias with `scrobbles` scrobbles at distinct instants."""
    a = TrackAlias(title=title, artist=artist, album=album)
    s.add(a)
    s.flush()
    for _ in range(scrobbles):
        when = datetime(2021, 1, 1, tzinfo=UTC) + timedelta(minutes=next(_clock))
        s.add(TrackAliasScrobble(alias_id=a.id, date=when, platform=Source.LASTFM))
    s.flush()
    return a


def test_library_excludes_apple_ms_only(session):
    ms = make_track(session, "MS only")
    _source(session, ms, Source.APPLE_MS)
    xml = make_track(session, "XML")
    _source(session, xml, Source.APPLE_XML)

    page = wanted_library(session)

    assert [r.track_id for r in page.rows] == [xml.id]
    assert page.total == 1
    assert page.rows[0].kind == "track"
    assert page.rows[0].sources == ["apple_xml"]


def test_library_excludes_groups_with_file_on_twin(session):
    canon = make_track(session, "C", date_added=_at(1))
    twin = make_track(session, "T", date_added=_at(2))
    _twin(session, twin, canon)
    _source(session, canon, Source.APPLE_XML)
    _file(session, twin)
    lone = make_track(session, "Lone", date_added=_at(3))
    _source(session, lone, Source.APPLE_XML)

    assert [r.track_id for r in wanted_library(session).rows] == [lone.id]


def test_library_sorted_oldest_first_and_resolves_to_root(session):
    new = make_track(session, "New", date_added=_at(5))
    _source(session, new, Source.APPLE_XML)
    canon = make_track(session, "Old", date_added=_at(1))
    twin = make_track(session, "Old twin", date_added=_at(2))
    _twin(session, twin, canon)
    _source(session, twin, Source.SPOTIFY)

    rows = wanted_library(session).rows

    assert [(r.track_id, r.title) for r in rows] == [(canon.id, "Old"), (new.id, "New")]


def test_signals_not_multiplied_by_joins(session):
    t = make_track(session, "Song", "Band", loved=True, rating=60)
    twin = make_track(session, "Song alt", "Band", rating=80)
    _twin(session, twin, t)
    _source(session, t, Source.APPLE_XML)
    _source(session, twin, Source.APPLE_MS)
    for name in ("a", "b"):
        pl = Playlist(name=name, platform=Source.APPLE_XML)
        session.add(pl)
        session.flush()
        session.add(PlaylistTrack(playlist_id=pl.id, track_id=t.id, position=0))
        session.add(PlaylistTrack(playlist_id=pl.id, track_id=twin.id, position=1))
    _plays(session, t, 2)
    _plays(session, twin, 1, start=_at(10))
    session.flush()

    (row,) = wanted_library(session).rows

    assert row.plays == 3
    assert row.playlists == 2
    assert row.sources == ["apple_ms", "apple_xml"]
    assert row.loved is True
    assert row.rating == 80
    assert row.first_played == _at(1)
    assert row.last_played == _at(10)
    assert row.alias_ids == []


def test_scrobbled_sorted_by_plays(session):
    five = make_track(session, "Five")
    _plays(session, five, 5)
    two = make_track(session, "Two")
    _plays(session, two, 2)
    alias = _alias(session, "Three", scrobbles=3)

    rows = wanted_scrobbled(session).rows

    assert [r.plays for r in rows] == [5, 3, 2]
    assert [r.kind for r in rows] == ["track", "unmatched", "track"]
    assert rows[1].alias_ids == [alias.id]
    assert rows[1].track_id is None
    assert (rows[1].playlists, rows[1].loved, rows[1].sources) == (0, False, [])


def test_scrobbled_excludes_filed_and_unplayed(session):
    filed = make_track(session, "Filed")
    _plays(session, filed, 3)
    _file(session, filed)
    make_track(session, "Unplayed")
    played = make_track(session, "Played")
    _plays(session, played, 1)

    rows = wanted_scrobbled(session, include_unmatched=False).rows

    assert [r.track_id for r in rows] == [played.id]


def test_unmatched_rows_collapse_aliases(session):
    a = _alias(session, "Song", "Band", album="One", scrobbles=1)
    b = _alias(session, "Song", "Band", album="Two", scrobbles=2)

    (row,) = wanted_scrobbled(session).rows

    assert row.kind == "unmatched"
    assert row.alias_ids == sorted([a.id, b.id])
    assert row.plays == 3
    assert row.album == "Two"  # from the alias with the most scrobbles


def test_unmatched_rows_blank_artist(session):
    _alias(session, "Song", artist=None)
    _alias(session, "Song", artist="x")

    rows = wanted_scrobbled(session).rows

    assert sorted(r.artist or "" for r in rows) == ["", "x"]


def test_unmatched_groups_include_rejected_and_skip_bound(session):
    rejected = _alias(session, "Gone", scrobbles=2)
    rejected.match_method = BindMethod.MANUAL
    bound = _alias(session, "Bound", scrobbles=4)
    bound.track_id = make_track(session, "Bound").id
    _alias(session, "Silent", scrobbles=0)
    session.flush()

    groups = unmatched_groups(session)

    assert [(g.alias_ids, g.scrobbles, g.rejected) for g in groups] == [([rejected.id], 2, True)]


def test_scrobbled_rows_carry_rejected(session):
    played = make_track(session, "Played")
    _plays(session, played, 9)
    gone = _alias(session, "Gone", "Band", scrobbles=3)
    gone.match_method = BindMethod.MANUAL  # rejected: unbound + MANUAL
    _alias(session, "Gone", "Band", album="Other", scrobbles=1)  # same row, merely unmatched
    _alias(session, "Plain", "Band", scrobbles=2)
    session.flush()

    rows = wanted_scrobbled(session).rows

    assert [(r.kind, r.title, r.rejected) for r in rows] == [
        ("track", "Played", False),
        ("unmatched", "Gone", True),
        ("unmatched", "Plain", False),
    ]


def test_skip_hidden_by_default(session):
    skipped = make_track(session, "Skipped")
    _plays(session, skipped, 3)
    set_track_verdict(session, skipped.id, Verdict.SKIP)
    wanted = make_track(session, "Wanted")
    _plays(session, wanted, 2)
    set_track_verdict(session, wanted.id, Verdict.WANTED)
    alias = _alias(session, "Alias skipped", scrobbles=1)
    set_alias_verdict(session, [alias.id], Verdict.SKIP)

    assert [r.track_id for r in wanted_scrobbled(session).rows] == [wanted.id]
    only_skip = wanted_scrobbled(session, verdict=Verdict.SKIP).rows
    assert [(r.kind, r.verdict) for r in only_skip] == [("track", Verdict.SKIP), ("unmatched", Verdict.SKIP)]
    assert [r.track_id for r in wanted_scrobbled(session, verdict=Verdict.WANTED).rows] == [wanted.id]


def test_unmatched_verdict_skip_wins_over_wanted(session):
    a = _alias(session, "Song", "Band", album="One")
    b = _alias(session, "Song", "Band", album="Two")
    set_alias_verdict(session, [a.id], Verdict.WANTED)
    set_alias_verdict(session, [b.id], Verdict.SKIP)

    assert wanted_scrobbled(session).rows == []
    (row,) = wanted_scrobbled(session, verdict=Verdict.SKIP).rows
    assert row.verdict == Verdict.SKIP


def test_query_filters_on_norms(session):
    hit = make_track(session, "Héllo World", "Band")
    _plays(session, hit, 1)
    miss = make_track(session, "Other", "Band")
    _plays(session, miss, 1)
    _alias(session, "Something", artist="WORLD Music")
    _alias(session, "100% pure", artist="y")

    rows = wanted_scrobbled(session, query="  WORLD ")

    assert sorted(r.title for r in rows.rows) == ["Héllo World", "Something"]
    assert [r.title for r in wanted_scrobbled(session, query="%").rows] == ["100% pure"]


def test_paging(session):
    for n in (3, 2, 1):
        _plays(session, make_track(session, f"T{n}"), n)

    first = wanted_scrobbled(session, limit=2)
    second = wanted_scrobbled(session, cursor=first.next_cursor, limit=2)

    assert ([r.plays for r in first.rows], first.total) == ([3, 2], 3)
    assert ([r.plays for r in second.rows], second.next_cursor) == ([1], None)


def test_skip_on_a_served_page_loses_no_row(session):
    """A row leaving the list after its page was served must not shift the next page (offset did)."""
    tracks = [make_track(session, f"T{n}") for n in (4, 3, 2, 1)]
    for t, n in zip(tracks, (4, 3, 2, 1), strict=True):
        _plays(session, t, n)

    first = wanted_scrobbled(session, limit=2)
    set_track_verdict(session, tracks[0].id, Verdict.SKIP)
    second = wanted_scrobbled(session, cursor=first.next_cursor, limit=2)

    assert [r.plays for r in first.rows] == [4, 3]
    assert ([r.plays for r in second.rows], second.next_cursor) == ([2, 1], None)


def test_library_pages_by_date_added_with_id_tiebreak(session):
    same_day = [make_track(session, f"L{i}", date_added=_at(5)) for i in range(3)]
    for t in same_day:
        _source(session, t, Source.SPOTIFY)

    first = wanted_library(session, limit=2)
    second = wanted_library(session, cursor=first.next_cursor, limit=2)

    assert [r.track_id for r in first.rows + second.rows] == [t.id for t in same_day]
    assert second.next_cursor is None


def test_cursor_of_another_list_is_refused(session):
    for n in (2, 1):
        _plays(session, make_track(session, f"T{n}"), n)
    cursor = wanted_scrobbled(session, limit=1).next_cursor

    with pytest.raises(ValueError, match="bad cursor"):
        wanted_library(session, cursor=cursor)


# "WyJ3YW50ZWRfbGlicmFyeSIsWyJ4Il1d" is ["wanted_library",["x"]]: the right list, a key of the wrong type.
@pytest.mark.parametrize("cursor", ["-1", "x", "1.5", "", " 2", "2", "WyJ3YW50ZWRfbGlicmFyeSIsWyJ4Il1d"])
def test_bad_cursor(session, cursor):
    _source(session, make_track(session, "L"), Source.SPOTIFY)
    with pytest.raises(ValueError, match="bad cursor"):
        wanted_library(session, cursor=cursor)


def test_limit_capped_at_200(session):
    for i in range(205):
        _plays(session, make_track(session, f"T{i}"), 1)

    page = wanted_scrobbled(session, limit=1000)

    assert (len(page.rows), page.total) == (200, 205)
    assert len(wanted_scrobbled(session, cursor=page.next_cursor, limit=1000).rows) == 5


def test_similar_with_file_flags_ghost(session):
    ghost = make_track(session, "Song (Remastered)", "Band", album="Best of")
    _source(session, ghost, Source.APPLE_XML)
    filed = make_track(session, "Song", "Band", album="Album")
    _file(session, filed)
    make_track(session, "Unrelated", "Nobody")

    (row,) = wanted_library(session).rows

    assert row.similar_with_file is not None
    assert row.similar_with_file["track_id"] == filed.id
    assert row.similar_with_file["title"] == "Song"
    assert row.similar_with_file["score"] >= 0.6


def test_similar_with_file_excludes_own_group_and_weak_matches(session):
    ghost = make_track(session, "Alpha", "Band")
    _source(session, ghost, Source.APPLE_XML)
    _file(session, make_track(session, "Zulu Quebec", "Someone Else"))

    (row,) = wanted_library(session).rows

    assert row.similar_with_file is None


def test_months_counts_distinct_months(session):
    t = make_track(session, "Song")
    for d in (
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    ):
        session.add(TrackPlay(track_id=t.id, played_at=d, platform=Source.LASTFM))
    session.flush()

    (row,) = wanted_scrobbled(session).rows

    assert row.months == 2


def test_similar_with_file_sees_file_on_twin(session):
    ghost = make_track(session, "Song (Remastered)", "Band", album="Best of")
    _source(session, ghost, Source.APPLE_XML)
    # The twin is created first, so it wins the id tiebreak unless the result resolves to the root.
    twin = make_track(session, "Song", "Band", album="Other", date_added=_at(2))
    canon = make_track(session, "Song", "Band", album="Album", date_added=_at(1))
    _twin(session, twin, canon)
    _file(session, twin)

    (row,) = wanted_library(session).rows

    assert row.track_id == ghost.id
    assert row.similar_with_file is not None
    assert row.similar_with_file["track_id"] == canon.id


def test_query_is_raw_for_track_rows(session):
    t = make_track(session, "Café Song", "Band")
    _source(session, t, Source.APPLE_XML)

    rows = wanted_library(session, query="Café").rows

    assert [r.track_id for r in rows] == [t.id]
