"""Tests for wanted/skip verdicts and their resolution onto canon groups."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from airdrome.enums import BindMethod, Verdict
from airdrome.library.edits import EditError, bind_aliases, mark_same
from airdrome.models import TrackAlias, WantedVerdict
from airdrome.wanted.verdicts import (
    alias_verdicts,
    clear_verdict,
    group_verdicts,
    set_alias_verdict,
    set_track_verdict,
)

from factories import make_track


def _at(day: int) -> datetime:
    """A fixed tz-aware UTC date, so `canon_order` is explicit in each test."""
    return datetime(2020, 1, day, tzinfo=UTC)


def _alias(s, title: str) -> TrackAlias:
    """A flushed, unbound alias."""
    a = TrackAlias(title=title, artist="x", album="")
    s.add(a)
    s.flush()
    return a


def _rows(s) -> int:
    """Number of stored verdict rows."""
    return s.scalar(select(func.count()).select_from(WantedVerdict))


def test_set_verdict_twice_keeps_one_row(session):
    t = make_track(session, "A")

    set_track_verdict(session, t.id, Verdict.WANTED, "first")
    row = set_track_verdict(session, t.id, Verdict.SKIP, "second")

    assert _rows(session) == 1
    assert (row.verdict, row.note, row.track_id) == (Verdict.SKIP, "second", t.id)


def test_verdict_on_twin_stored_on_canon(session):
    canon = make_track(session, "C", date_added=_at(1))
    twin = make_track(session, "T", date_added=_at(2))
    twin.canon = canon
    twin.canon_method = BindMethod.DEDUP_FLAGS
    session.flush()

    row = set_track_verdict(session, twin.id, Verdict.WANTED)

    assert row.track_id == canon.id
    assert group_verdicts(session) == {canon.id: Verdict.WANTED}


def test_verdict_follows_group_after_merge(session):
    a = make_track(session, "A", "x", date_added=_at(1))
    b = make_track(session, "B", "y", date_added=_at(2))
    set_track_verdict(session, b.id, Verdict.WANTED)

    mark_same(session, a.id, b.id)

    assert group_verdicts(session) == {a.id: Verdict.WANTED}


def test_skip_wins(session):
    a = make_track(session, "A", "x", date_added=_at(1))
    b = make_track(session, "B", "y", date_added=_at(2))
    set_track_verdict(session, a.id, Verdict.WANTED)
    set_track_verdict(session, b.id, Verdict.SKIP)

    mark_same(session, a.id, b.id)

    assert group_verdicts(session) == {a.id: Verdict.SKIP}


def test_set_verdict_replaces_member_verdicts(session):
    a = make_track(session, "A", "x", date_added=_at(1))
    b = make_track(session, "B", "y", date_added=_at(2))
    alias = _alias(session, "bound")
    set_track_verdict(session, b.id, Verdict.SKIP)
    set_alias_verdict(session, [alias.id], Verdict.SKIP)
    mark_same(session, a.id, b.id)
    bind_aliases(session, [alias.id], a.id)
    assert group_verdicts(session) == {a.id: Verdict.SKIP}

    row = set_track_verdict(session, b.id, Verdict.WANTED)

    assert row.track_id == a.id
    assert _rows(session) == 1
    assert group_verdicts(session) == {a.id: Verdict.WANTED}


def test_alias_verdict_moves_on_bind(session):
    t = make_track(session, "T")
    alias = _alias(session, "al")
    set_alias_verdict(session, [alias.id], Verdict.SKIP)
    assert alias_verdicts(session) == {alias.id: Verdict.SKIP}
    assert group_verdicts(session) == {}

    bind_aliases(session, [alias.id], t.id)

    assert group_verdicts(session) == {t.id: Verdict.SKIP}
    assert alias_verdicts(session) == {}


def test_set_alias_verdict_upserts_one_row_each(session):
    a1, a2 = _alias(session, "a1"), _alias(session, "a2")

    set_alias_verdict(session, [a1.id, a2.id], Verdict.WANTED)
    rows = set_alias_verdict(session, [a1.id], Verdict.SKIP, "no")

    assert _rows(session) == 2
    assert [(r.alias_id, r.verdict, r.note) for r in rows] == [(a1.id, Verdict.SKIP, "no")]
    assert alias_verdicts(session) == {a1.id: Verdict.SKIP, a2.id: Verdict.WANTED}


def test_clear_verdict(session):
    a = make_track(session, "A", "x", date_added=_at(1))
    b = make_track(session, "B", "y", date_added=_at(2))
    alias = _alias(session, "al")
    set_track_verdict(session, a.id, Verdict.WANTED)
    set_track_verdict(session, b.id, Verdict.SKIP)
    set_alias_verdict(session, [alias.id], Verdict.WANTED)
    mark_same(session, a.id, b.id)

    assert clear_verdict(session, track_id=b.id) == 2
    assert clear_verdict(session, alias_ids=[alias.id]) == 1
    assert clear_verdict(session, alias_ids=[alias.id]) == 0
    assert _rows(session) == 0


def test_unknown_ids_raise(session):
    with pytest.raises(EditError):
        set_track_verdict(session, 999_999, Verdict.WANTED)
    with pytest.raises(EditError):
        set_alias_verdict(session, [999_999], Verdict.WANTED)
    with pytest.raises(EditError):
        clear_verdict(session, track_id=999_999)
    with pytest.raises(EditError):
        clear_verdict(session, alias_ids=[999_999])


def test_group_verdicts_unmarked_tracks_absent(session):
    make_track(session, "A")
    assert group_verdicts(session) == {}


def test_clear_track_verdict_clears_bound_alias_verdicts(session):
    t = make_track(session, "T")
    alias = _alias(session, "al")
    set_alias_verdict(session, [alias.id], Verdict.SKIP)
    set_track_verdict(session, t.id, Verdict.WANTED)
    bind_aliases(session, [alias.id], t.id)

    assert clear_verdict(session, track_id=t.id) == 2
    assert group_verdicts(session) == {}
    assert _rows(session) == 0


def test_duplicate_alias_ids_make_one_row(session):
    alias = _alias(session, "al")

    rows = set_alias_verdict(session, [alias.id, alias.id], Verdict.WANTED)

    assert len(rows) == 1
    assert _rows(session) == 1
