"""Pair decision keys and the pure newest-wins resolver (no DB)."""

import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest

from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.normalize.dedup.decisions import (
    MIRROR_VERSION,
    Canon,
    Outcome,
    Pair,
    Resolution,
    dump_v2,
    is_legacy,
    key_text,
    load_v2,
    pair,
    parse_key,
    resolve,
)


T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1, T2, T3 = (T0 + timedelta(days=i) for i in (1, 2, 3))
SAME, DIFF, MCP = DecisionKind.SAME, DecisionKind.DIFFERENT, DecisionOrigin.MCP


def mk(x: str, y: str, kind: DecisionKind, at: datetime) -> Pair:
    """Build a pair decision with origin MCP."""
    return pair(x, y, kind, at, MCP)


def test_key_text_round_trips_and_keeps_none_apart_from_empty() -> None:
    """None and "" stay distinct and the key survives a text round trip."""
    assert parse_key(key_text(("t", None, "", None))) == ("t", None, "", None)
    assert key_text(("t", None, None, None)) != key_text(("t", "", None, None))
    assert parse_key(["t", "a", None, "x"]) == ("t", "a", None, "x")


@pytest.mark.parametrize("bad", [["t", "a", "b"], [None, "a", "b", "c"], ["t", 1, None, None], "{}", "[1]"])
def test_parse_key_rejects_malformed(bad: str | list) -> None:
    """A key must be a str title plus three str | None."""
    with pytest.raises(ValueError):
        parse_key(bad)


def test_pair_orders_ends_and_rejects_self_and_naive() -> None:
    """Ends are ordered; identical ends and naive times raise."""
    assert pair("b", "a", SAME, T1, MCP).a == "a"
    with pytest.raises(ValueError):
        pair("x", "x", SAME, T1, MCP)
    with pytest.raises(ValueError):
        pair("x", "y", SAME, datetime(2026, 1, 1), MCP)


def test_repro_both_read_orders_agree() -> None:
    """Row order never changes the resolution."""
    v, w, x = "v", "w", "x"
    live = {v: 1, w: 2, x: 3}
    p1, p2 = mk(w, v, SAME, T1), mk(x, w, SAME, T2)
    r1, r2 = resolve([p1, p2], [], live), resolve([p2, p1], [], live)
    assert r1 == r2
    assert r1.components == [frozenset({1, 2, 3})]


def test_newest_different_supersedes_older_chain() -> None:
    """A newer different breaks the oldest link of an older same chain."""
    live = {"a": 1, "b": 2, "c": 3}
    r = resolve([mk("a", "b", SAME, T1), mk("b", "c", SAME, T2), mk("a", "c", DIFF, T3)], [], live)
    assert r.components == [frozenset({2, 3})]
    assert frozenset({1, 2}) in r.cannot_link
    assert frozenset({1, 3}) in r.cannot_link
    assert r.outcomes[("a", "b")] == Outcome.SUPERSEDED
    assert r.outcomes[("b", "c")] == Outcome.ACCEPTED
    assert r.outcomes[("a", "c")] == Outcome.ACCEPTED


def test_newest_same_supersedes_older_different() -> None:
    """A newer same chain joins tracks an older different separated."""
    live = {"a": 1, "b": 2, "c": 3}
    r = resolve([mk("a", "c", DIFF, T1), mk("a", "b", SAME, T2), mk("b", "c", SAME, T3)], [], live)
    assert r.components == [frozenset({1, 2, 3})]
    assert r.cannot_link == frozenset()
    assert r.outcomes[("a", "c")] == Outcome.SUPERSEDED


def test_separation_merges_with_components() -> None:
    """A separation follows its track into a merged component."""
    live = {"a": 1, "x": 2, "y": 3}
    r = resolve([mk("a", "x", DIFF, T3), mk("x", "y", SAME, T2), mk("a", "y", SAME, T1)], [], live)
    assert r.outcomes[("a", "y")] == Outcome.SUPERSEDED
    assert r.components == [frozenset({2, 3})]
    assert r.cannot_link == frozenset({frozenset({1, 2}), frozenset({1, 3})})


def test_dead_decision_ignored_and_counted() -> None:
    """A pair with a missing end is DEAD and joins nothing; a canon on a missing key is counted."""
    live = {"a": 1}
    r = resolve([mk("a", "gone", SAME, T1)], [Canon("gone", T1, MCP)], live)
    assert r.outcomes[("a", "gone")] == Outcome.DEAD
    assert r.components == []
    assert r.dead_canons == 1


def test_ties_resolve_identically_in_both_input_orders() -> None:
    """Equal timestamps resolve identically in both input orders (same wins the tie, see resolve)."""
    live = {"a": 1, "b": 2, "c": 3}
    ps = [mk("a", "b", SAME, T1), mk("b", "c", SAME, T1), mk("a", "c", DIFF, T1)]
    assert resolve(ps, [], live) == resolve(ps[::-1], [], live)
    first = resolve(ps, [], live)
    assert first.outcomes[("a", "b")] == Outcome.ACCEPTED
    assert first.outcomes[("b", "c")] == Outcome.ACCEPTED
    assert first.outcomes[("a", "c")] == Outcome.SUPERSEDED


@pytest.mark.parametrize("prefix", ["", "z-"])  # "z-" sorts the tied different pair before the same one
def test_equal_time_same_beats_different_in_every_input_order(prefix: str) -> None:
    """At one timestamp `same` is applied before `different`: the older group's join survives."""
    explicit, course, ep = "Explicit", prefix + "Course", prefix + "EP"
    live = {explicit: 1, course: 2, ep: 3}
    ps = [mk(ep, course, SAME, T1), mk(explicit, ep, DIFF, T1), mk(explicit, course, SAME, T2)]
    for perm in itertools.permutations(ps):
        r = resolve(perm, [], live)
        assert r.components == [frozenset({1, 2, 3})]
        assert r.cannot_link == frozenset()
        assert r.outcomes[tuple(sorted((explicit, ep)))] == Outcome.SUPERSEDED
        assert r.outcomes[tuple(sorted((ep, course)))] == Outcome.ACCEPTED
        assert r.outcomes[tuple(sorted((explicit, course)))] == Outcome.ACCEPTED


def test_canons_map_live_tracks() -> None:
    """A live canon key maps to its track id; the newest choice wins."""
    r = resolve([], [Canon("k", T1, MCP), Canon("k", T2, MCP)], {"k": 7})
    assert r == Resolution([], frozenset(), {7: T2}, {}, 0)


def _sample() -> tuple[list[Pair], list[Canon]]:
    """Return pairs and canons covering None fields and a non-ASCII title."""
    k1 = key_text(("Café", None, "Album", None))
    k2 = key_text(("Zed", "Artist", None, "AA"))
    k3 = key_text(("Alpha", "A", "B", "C"))
    ps = [pair(k1, k2, SAME, T1, MCP), pair(k3, k2, DIFF, T2, DecisionOrigin.MIGRATED)]
    cs = [Canon(k2, T3, MCP), Canon(k1, T1, DecisionOrigin.IMPORT)]
    return ps, cs


def test_v2_round_trip() -> None:
    """Load(dump) returns the input sorted, whatever the input order."""
    ps, cs = _sample()
    doc = dump_v2(ps, cs)
    assert doc == dump_v2(reversed(ps), reversed(cs))
    got_p, got_c = load_v2(json.loads(json.dumps(doc, ensure_ascii=False)))
    assert got_p == sorted(ps, key=lambda p: (p.a, p.b))
    assert got_c == sorted(cs, key=lambda c: c.track)
    assert doc["version"] == MIRROR_VERSION == 2
    assert doc["pairs"][0]["a"] == list(parse_key(got_p[0].a))


def test_v2_empty_document() -> None:
    """An empty input still names its version."""
    assert dump_v2([], []) == {"version": 2, "pairs": [], "canons": []}


GOOD_PAIR = {"a": ["a", None, None, None], "b": ["b", None, None, None], "kind": "same", "origin": "mcp"}


def _doc(**over: object) -> dict:
    """Build a one-pair v2 document with `over` applied to the pair."""
    return {
        "version": 2,
        "pairs": [{**GOOD_PAIR, "decided_at": "2026-01-01T00:00:00+00:00", **over}],
        "canons": [],
    }


@pytest.mark.parametrize(
    "data",
    [
        {**_doc(), "version": 3},
        {k: v for k, v in _doc().items() if k != "version"},
        _doc(a=["a", None, None]),
        _doc(kind="maybe"),
        _doc(origin="robot"),
        _doc(decided_at="2026-01-01T00:00:00"),
        _doc(decided_at="yesterday"),
        {
            "version": 2,
            "pairs": [],
            "canons": [
                {"track": ["t", None, None, None], "decided_at": "2026-01-01T00:00:00", "origin": "mcp"}
            ],
        },
    ],
)
def test_load_v2_rejects_bad_input(data: dict) -> None:
    """A wrong version, key, kind, origin or naive time is a ValueError."""
    with pytest.raises(ValueError):
        load_v2(data)


def test_is_legacy() -> None:
    """Only a dict without a version is the old shape."""
    assert is_legacy({}) and is_legacy({"g": {"members": []}})
    assert not is_legacy({"version": 2}) and not is_legacy([])
