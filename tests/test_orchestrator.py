"""Multi-remote reconcile orchestrator tests.

Drive `reconcile` end to end with the in-memory `FakeBackend`/`FakeSource` from the sync
tests: a clean multi-remote auto pass, the resolver firing on a real conflict (take a
remote / abort), and a single read-only pull. The pairwise merge and the resolver are
unit-tested elsewhere; here we check the orchestration wiring around them.
"""

import pytest
from test_playlist_sync import FakeBackend, FakeSource, _playlist, _pt_rows, _seed_link

from airdrome.enums import Source
from airdrome.playlists import resolver_tui
from airdrome.playlists.orchestrator import reconcile

from factories import make_track


@pytest.fixture()
def no_prompt(monkeypatch):
    """Fail if the resolver opens — used to assert a pass was fully automatic."""
    monkeypatch.setattr(
        resolver_tui.Prompt, "ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolver opened"))
    )


@pytest.fixture()
def scripted(monkeypatch):
    from collections import deque

    def _install(*keys: str):
        q = deque(keys)
        monkeypatch.setattr(resolver_tui.Prompt, "ask", lambda *a, **k: q.popleft())

    return _install


def test_multi_remote_auto_merges_without_resolver(session, no_prompt):
    """Pure adds from two remotes union into canonical with no conflict prompt."""
    x, y = make_track(session, "x"), make_track(session, "y")
    be = FakeBackend()
    be.register(x.id, "r1")
    be.register(y.id, "r2")
    be.seed("e", "P", ["r1", "r2"])  # backend added y since base
    src = FakeSource()
    src.canon_of["s1"] = x.id
    src.seed("sp", "P", ["s1"])  # source unchanged
    pl = _playlist(session, [x])
    _seed_link(session, pl, "e", [x.id], Source.NAVIDROME)
    _seed_link(session, pl, "sp", [x.id], Source.APPLE_XML)

    reconcile(session, [src, be])

    assert set(_pt_rows(session, pl)) == {x.id, y.id}


def test_single_source_pulls_into_canonical(session, no_prompt):
    """`sync apple_xml`-style single read-only pass merges the source in, no prompt."""
    x = make_track(session, "x")
    src = FakeSource()
    src.canon_of["s1"] = x.id
    src.seed("sp", "P", ["s1"])
    pl = _playlist(session, [])
    _seed_link(session, pl, "sp", [], Source.APPLE_XML)

    reconcile(session, [src])

    assert _pt_rows(session, pl) == [x.id]


def test_conflict_opens_resolver_and_take_remote_wins(session, scripted):
    """Source added X, backend removed X -> conflict; take the source, X is kept + pushed."""
    x = make_track(session, "x")
    be = FakeBackend()
    be.register(x.id, "r1")
    be.seed("e", "P", [])  # backend removed x (base had it)
    src = FakeSource()
    src.canon_of["s1"] = x.id
    src.seed("sp", "P", ["s1"])  # source added x (base empty)
    pl = _playlist(session, [x])
    _seed_link(session, pl, "e", [x.id], Source.NAVIDROME)
    _seed_link(session, pl, "sp", [], Source.APPLE_XML)

    scripted("1", "c")  # take remote 1 (the source), then commit
    reconcile(session, [src, be])

    assert _pt_rows(session, pl) == [x.id]  # source's view won
    assert be.tracks["e"] == ["r1"]  # and was pushed back to the backend


def test_conflict_abort_changes_nothing(session, scripted):
    x = make_track(session, "x")
    be = FakeBackend()
    be.register(x.id, "r1")
    be.seed("e", "P", [])
    src = FakeSource()
    src.canon_of["s1"] = x.id
    src.seed("sp", "P", ["s1"])
    pl = _playlist(session, [x])
    _seed_link(session, pl, "e", [x.id], Source.NAVIDROME)
    _seed_link(session, pl, "sp", [], Source.APPLE_XML)
    session.commit()  # as in production: land committed the playlist/links before sync runs

    scripted("q")  # abort
    reconcile(session, [src, be])

    assert _pt_rows(session, pl) == [x.id]  # canonical untouched
    assert be.tracks["e"] == []  # backend untouched
