"""Tests for the track card and the unmatched-alias list with ranked candidates."""

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from airdrome.cloud.sources import SourceTrack
from airdrome.enums import BindMethod, DecisionKind, Source, Verdict
from airdrome.library.card import track_card, unmatched_aliases
from airdrome.library.edits import EditError, bind_aliases
from airdrome.match import find_best_match, find_matches
from airdrome.models import (
    Playlist,
    PlaylistTrack,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
)
from airdrome.wanted.verdicts import set_track_verdict

from factories import add_canon, add_pair, make_track


_ids = count(1)
_clock = count()


def _alias(
    s, title: str, artist: str | None = "band", album: str | None = "", scrobbles: int = 1
) -> TrackAlias:
    """A flushed unbound alias with `scrobbles` scrobbles at distinct instants."""
    a = TrackAlias(title=title, artist=artist, album=album)
    s.add(a)
    s.flush()
    for _ in range(scrobbles):
        when = datetime(2022, 1, 1, tzinfo=UTC) + timedelta(minutes=next(_clock))
        s.add(TrackAliasScrobble(alias_id=a.id, date=when, platform=Source.LASTFM))
    s.flush()
    return a


def _group(s) -> tuple[Track, Track]:
    """A canon and one twin."""
    canon = make_track(s, "Song", "Band", album="A")
    twin = make_track(s, "Song", "Band", album="B")
    twin.canon = canon
    twin.canon_method = BindMethod.DEDUP_FLAGS
    twin.canon_confidence = 0.9
    s.flush()
    return canon, twin


def test_card_from_twin_shows_whole_group(session):
    canon, twin = _group(session)
    session.add(SourceTrack(provider=Source.APPLE_XML, source_id="1", track_id=twin.id, title="Song"))
    session.add(TrackFile(source_path=Path("/m/1.mp3"), track_id=canon.id, is_main=True, bitrate=320))
    session.add(
        TrackPlay(track_id=twin.id, played_at=datetime(2022, 1, 1, tzinfo=UTC), platform=Source.LASTFM)
    )
    pl = Playlist(name="Mix", platform=Source.APPLE_XML, source_id="p")
    session.add(pl)
    session.flush()
    session.add(PlaylistTrack(playlist_id=pl.id, track_id=twin.id, position=0))
    alias = _alias(session, "Song", "Band", scrobbles=3)
    alias.track_id = canon.id
    alias.match_method = BindMethod.MANUAL
    add_pair(session, canon, twin, DecisionKind.SAME, at=datetime(2021, 1, 1, tzinfo=UTC))
    set_track_verdict(session, canon.id, Verdict.WANTED)
    session.flush()

    card = track_card(session, twin.id)

    assert card["canon"]["id"] == canon.id
    assert [m["id"] for m in card["members"]] == [canon.id, twin.id]
    by_id = {m["id"]: m for m in card["members"]}
    assert by_id[twin.id]["canon_method"] == "dedup_flags"
    assert by_id[twin.id]["sources"] == [{"provider": "apple_xml", "source_id": "1"}]
    assert by_id[canon.id]["files"][0]["bitrate"] == 320
    assert card["plays"] == 1
    assert card["playlists"] == [{"id": pl.id, "name": "Mix"}]
    assert card["aliases_total"] == 1
    assert card["aliases"][0]["scrobbles"] == 3
    assert card["verdict"] == "wanted"
    [decided] = card["decisions"]["pairs"]
    assert {decided["a"]["track_id"], decided["b"]["track_id"]} == {canon.id, twin.id}
    assert (decided["kind"], decided["status"]) == ("same", "accepted")
    assert card["decisions"]["canons"] == []
    json.dumps(card)


def _at(day: int) -> datetime:
    """A decision time on `day` of January 2021."""
    return datetime(2021, 1, day, tzinfo=UTC)


def test_card_lists_decisions_with_status(session):
    canon, twin = _group(session)
    other = make_track(session, "Other", "Band")
    gone = make_track(session, "Gone", "Band")
    far = make_track(session, "Far", "Elsewhere")
    add_pair(session, canon, other, DecisionKind.SAME, at=_at(1))  # superseded by the newer different
    add_pair(session, twin, other, DecisionKind.DIFFERENT, at=_at(2))  # accepted
    add_pair(session, canon, gone, DecisionKind.SAME, at=_at(3))  # dead once `gone` is deleted
    add_pair(session, other, far, DecisionKind.SAME, at=_at(4))  # touches no member: not listed
    add_pair(session, canon, twin, DecisionKind.SAME, at=_at(5))  # accepted
    add_canon(session, twin, at=_at(6))
    gone_key = [gone.title, gone.artist, gone.album, gone.album_artist]
    session.delete(gone)
    session.flush()

    decisions = track_card(session, canon.id)["decisions"]

    rows = [(p["kind"], p["status"], p["decided_at"]) for p in decisions["pairs"]]
    assert rows == [
        ("same", "accepted", _at(5).isoformat()),
        ("same", "dead", _at(3).isoformat()),
        ("different", "accepted", _at(2).isoformat()),
        ("same", "superseded", _at(1).isoformat()),
    ]
    dead = decisions["pairs"][1]
    ends = {(tuple(e["key"]), e["track_id"]) for e in (dead["a"], dead["b"])}
    assert (tuple(gone_key), None) in ends
    assert ("Song", "Band", "A", None) in {k for k, _ in ends}
    assert decisions["canons"] == [
        {
            "key": ["Song", "Band", "B", None],
            "track_id": twin.id,
            "decided_at": _at(6).isoformat(),
            "origin": "mcp",
            "status": "accepted",
        }
    ]
    assert track_card(session, make_track(session, "Lone", "Band").id)["decisions"] == {
        "pairs": [],
        "canons": [],
    }
    json.dumps(decisions)


def test_card_unknown_id_raises(session):
    with pytest.raises(EditError):
        track_card(session, 999_999)


def test_card_counts_survive_a_bind_in_the_same_session(session):
    canon = make_track(session, "Song", "Band")
    alias = _alias(session, "Song", "Band", scrobbles=2)
    assert track_card(session, canon.id)["plays"] == 0

    bind_aliases(session, [alias.id], canon.id)
    card = track_card(session, canon.id)

    assert card["plays"] == 2
    assert card["aliases_total"] == 1


def test_card_alias_list_capped_at_50(session):
    canon = make_track(session, "Song", "Band")
    for i in range(55):
        a = _alias(session, f"Song {i}", "Band")
        a.track_id = canon.id
    session.flush()

    card = track_card(session, canon.id)

    assert len(card["aliases"]) == 50
    assert card["aliases_total"] == 55


def test_unmatched_candidates_ranked(session):
    make_track(session, "song", "other")
    make_track(session, "song", "band")
    _alias(session, "song", "band", scrobbles=2)

    page = unmatched_aliases(session)

    (row,) = page["rows"]
    assert row["scrobbles"] == 2
    cands = row["candidates"]
    assert cands[0]["artist"] == "band"
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True)
    assert page["total"] == 1
    assert page["next_cursor"] is None
    json.dumps(page)


def test_unmatched_candidates_collapse_twins_to_root(session):
    canon, _twin = _group(session)
    _alias(session, "Song", "Band")

    (row,) = unmatched_aliases(session)["rows"]

    assert [c["track_id"] for c in row["candidates"]] == [canon.id]


def test_unmatched_paging_and_bad_cursor(session):
    for i in range(3):
        _alias(session, f"zzz{i}", "q", scrobbles=3 - i)

    first = unmatched_aliases(session, limit=2)
    assert len(first["rows"]) == 2
    assert first["next_cursor"] is not None
    second = unmatched_aliases(session, cursor=first["next_cursor"], limit=2)
    assert len(second["rows"]) == 1
    assert second["next_cursor"] is None
    with pytest.raises(ValueError, match="bad cursor"):
        unmatched_aliases(session, cursor="x")


def test_find_best_match_unchanged(session):
    make_track(session, "song", "other")
    best = make_track(session, "song", "band")

    one = find_best_match(session, "song", "band", "")
    many = find_matches(session, "song", "band", "")

    assert one is not None
    assert one[0].id == best.id
    assert many[0] == one
    assert find_best_match(session, "nothing here", "nobody", "") is None
    assert find_matches(session, "nothing here", "nobody", "") == []
