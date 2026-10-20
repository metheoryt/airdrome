"""The decision tables, the Track key under NULLS NOT DISTINCT, and the store's upsert/resolve helpers."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy.exc import IntegrityError

from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.library.ladder import upsert_track
from airdrome.models import PairDecision, Track
from airdrome.normalize.dedup.decisions import Canon, Outcome, Pair, key_text, load_v2, pair, track_key
from airdrome.normalize.dedup.store import (
    Upsert,
    export_decisions,
    live_keys,
    load_canons,
    load_pairs,
    resolve_library,
    upsert_canon,
    upsert_pair,
)

from factories import add_canon, add_pair, make_track


T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
T3 = T2 + timedelta(days=1)


def test_track_key_is_unique_with_nulls(session):
    """Two tracks with only a title collide: NULL key fields are not distinct."""
    session.add(Track(title="x"))
    session.flush()
    session.add(Track(title="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_get_or_create_finds_null_field_row(session):
    """Regression guard: a NULL-field key is looked up with IS NULL, so the second call finds the row."""
    kw = {"title": "x", "artist": None, "album": None, "album_artist": None}
    first, created1 = Track.get_or_create(session, **kw)
    second, created2 = Track.get_or_create(session, **kw)
    assert first is second
    assert (created1, created2) == (True, False)
    third, created3, _ = upsert_track(session, defaults={}, **kw)
    assert third is first
    assert not created3


def _keys(session):
    a = make_track(session, title="a")
    b = make_track(session, title="b")
    return a, b, key_text(track_key(a)), key_text(track_key(b))


def test_upsert_pair_newest_wins_never_older(session):
    _, _, ka, kb = _keys(session)
    assert upsert_pair(session, pair(ka, kb, DecisionKind.SAME, T2, DecisionOrigin.MCP)) is Upsert.CREATED
    # older: kept, row unchanged (ends given in the other order hit the same row)
    assert upsert_pair(session, pair(kb, ka, DecisionKind.DIFFERENT, T1, DecisionOrigin.MCP)) is Upsert.KEPT
    # equal: kept
    assert upsert_pair(session, pair(ka, kb, DecisionKind.DIFFERENT, T2, DecisionOrigin.MCP)) is Upsert.KEPT
    (row,) = load_pairs(session)
    assert (row.kind, row.decided_at) == (DecisionKind.SAME, T2)
    # strictly newer, other kind: replaced
    assert (
        upsert_pair(session, pair(kb, ka, DecisionKind.DIFFERENT, T3, DecisionOrigin.IMPORT))
        is Upsert.REPLACED
    )
    (row,) = load_pairs(session)
    assert (row.kind, row.decided_at, row.origin) == (DecisionKind.DIFFERENT, T3, DecisionOrigin.IMPORT)


def test_upsert_canon_newest_wins(session):
    _, _, ka, _ = _keys(session)
    assert upsert_canon(session, Canon(ka, T2, DecisionOrigin.MCP)) is Upsert.CREATED
    assert upsert_canon(session, Canon(ka, T1, DecisionOrigin.IMPORT)) is Upsert.KEPT
    assert upsert_canon(session, Canon(ka, T2, DecisionOrigin.IMPORT)) is Upsert.KEPT
    (row,) = load_canons(session)
    assert (row.decided_at, row.origin) == (T2, DecisionOrigin.MCP)
    assert upsert_canon(session, Canon(ka, T3, DecisionOrigin.IMPORT)) is Upsert.REPLACED
    (row,) = load_canons(session)
    assert (row.decided_at, row.origin) == (T3, DecisionOrigin.IMPORT)


def test_loaders_sort_in_python_by_key_text(session):
    ts = [make_track(session, title=t) for t in ("b", "C", "a", "B")]
    for x, y in pairwise(ts):
        add_pair(session, x, y)
    for t in ts:
        add_canon(session, t)
    pairs, canons = load_pairs(session), load_canons(session)
    assert pairs == sorted(pairs, key=lambda p: (p.a, p.b))
    assert canons == sorted(canons, key=lambda c: c.track)


def test_export_decisions_round_trips(session):
    a, b, _, _ = _keys(session)
    add_pair(session, a, b, DecisionKind.DIFFERENT)
    add_canon(session, a)
    assert load_v2(export_decisions(session)) == (load_pairs(session), load_canons(session))


def test_factory_default_times_are_past_and_increasing(session):
    a, b, _, _ = _keys(session)
    p = add_pair(session, a, b)
    c1, c2 = add_canon(session, a), add_canon(session, b)
    assert p.decided_at < c1.decided_at < c2.decided_at < datetime(2021, 1, 1, tzinfo=UTC)


def test_live_keys_maps_key_text_to_id(session):
    a, b, ka, kb = _keys(session)
    assert live_keys(session) == {ka: a.id, kb: b.id}


def test_resolve_library_sees_live_tracks(session):
    a, b, ka, _ = _keys(session)
    add_pair(session, a, b)
    dead = key_text(("nobody", None, None, None))
    lo, hi = sorted((ka, dead))
    session.add(
        PairDecision(key_a=lo, key_b=hi, kind=DecisionKind.SAME, decided_at=T1, origin=DecisionOrigin.MCP)
    )
    session.flush()
    res = resolve_library(session)
    assert res.components == [frozenset({a.id, b.id})]
    assert list(res.outcomes.values()).count(Outcome.DEAD) == 1


def test_models_have_canon_folded_default_false(session):
    t = make_track(session, title="f")
    session.refresh(t)
    assert t.canon_folded is False


def test_upsert_pair_rejects_unordered_pair(session):
    """A directly built Pair with a > b must not create a second row for the same unordered pair."""
    _, _, ka, kb = _keys(session)
    ordered = pair(ka, kb, DecisionKind.SAME, T1, DecisionOrigin.MCP)
    upsert_pair(session, ordered)
    backwards = Pair(ordered.b, ordered.a, DecisionKind.DIFFERENT, T3, DecisionOrigin.MCP)
    with pytest.raises(ValueError):
        upsert_pair(session, backwards)
    assert load_pairs(session) == [ordered]
