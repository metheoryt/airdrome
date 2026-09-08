"""Multi-remote reconcile orchestrator tests.

Drive `reconcile` end to end with the in-memory `FakeBackend`/`FakeSource` from the sync
tests: a clean multi-remote auto pass, a single read-only pull, and a real add-vs-remove
conflict settled automatically by the last remote that edited the track. `reconcile`
never prompts, so reconcile *order* is the whole decision — both directions are pinned
here. The pairwise merge and `resolve_latest` are unit-tested elsewhere; this file checks
the orchestration wiring around them.
"""

from test_playlist_sync import FakeBackend, FakeSource, _playlist, _pt_rows, _seed_link

from airdrome.enums import Source
from airdrome.playlists.orchestrator import reconcile

from factories import make_track


def _conflicting_pair(session):
    """Source added X (base empty); backend removed X (base had it). One track, one conflict."""
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
    return x, src, be, pl


def test_multi_remote_auto_merges(session):
    """Pure adds from two remotes union into canonical — no conflict, nothing to decide."""
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


def test_single_source_pulls_into_canonical(session):
    """`sync apple_xml`-style single read-only pass merges the source in."""
    x = make_track(session, "x")
    src = FakeSource()
    src.canon_of["s1"] = x.id
    src.seed("sp", "P", ["s1"])
    pl = _playlist(session, [])
    _seed_link(session, pl, "sp", [], Source.APPLE_XML)

    reconcile(session, [src])

    assert _pt_rows(session, pl) == [x.id]


def test_conflict_goes_to_the_last_remote_that_edited_the_track(session, capsys):
    """Source first, backend last: the backend's removal wins and canonical loses X."""
    _x, src, be, pl = _conflicting_pair(session)

    reconcile(session, [src, be])

    assert _pt_rows(session, pl) == []
    assert be.tracks["e"] == []
    # the resolution is reported by track title, not by canonical id
    assert "P: x → navidrome" in capsys.readouterr().out


def test_conflict_reverses_with_reconcile_order(session):
    """Backend first, source last: the source's add wins and is pushed to the backend."""
    x, src, be, pl = _conflicting_pair(session)

    reconcile(session, [be, src])

    assert _pt_rows(session, pl) == [x.id]
    assert be.tracks["e"] == ["r1"]
