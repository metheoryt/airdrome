"""PlaylistConflictUI loop tests — drive serve() with scripted keypresses.

Like the dedup TUI tests, `Prompt.ask` is monkeypatched with a fixed script so the
per-playlist decision collection can be asserted without a terminal. Rendering runs
for real (it just prints), so this also smoke-tests the table/label composition.
"""

from collections import deque

import pytest

from airdrome.enums import Source
from airdrome.playlists import resolver_tui
from airdrome.playlists.conflicts import Decision, PlaylistConflict, RemoteState, Strategy, detect_conflicts
from airdrome.playlists.resolver_tui import PlaylistConflictUI

from factories import make_track


@pytest.fixture()
def scripted(monkeypatch):
    """Feed serve()'s Prompt.ask a queue of inputs."""

    def _install(*keys: str):
        q = deque(keys)
        monkeypatch.setattr(resolver_tui.Prompt, "ask", lambda *a, **k: q.popleft())

    return _install


def _conflict(session, name: str, ours, states) -> PlaylistConflict:
    pl_id = make_track(session, name).id  # any stable int id; not a real playlist row
    return PlaylistConflict(
        playlist_id=pl_id,
        playlist_name=name,
        ours=ours,
        states=states,
        conflicts=detect_conflicts(states),
    )


def test_commit_with_no_input_defaults_every_playlist_to_auto(session, scripted):
    x = make_track(session, "x").id
    c = _conflict(
        session,
        "P",
        ours=[x],
        states=[RemoteState(Source.APPLE_XML, [], [x]), RemoteState(Source.NAVIDROME, [x], [])],
    )
    scripted("c")  # commit immediately
    out = PlaylistConflictUI(session, [c]).serve()
    assert out == {c.playlist_id: Decision(Strategy.AUTO)}


def test_quit_aborts_returns_none(session, scripted):
    x = make_track(session, "x").id
    c = _conflict(session, "P", ours=[x], states=[RemoteState(Source.NAVIDROME, [x], [])])
    scripted("q")
    assert PlaylistConflictUI(session, [c]).serve() is None


def test_collects_per_playlist_choices(session, scripted):
    x, y = make_track(session, "x").id, make_track(session, "y").id
    c1 = _conflict(
        session,
        "A",
        ours=[x],
        states=[RemoteState(Source.APPLE_XML, [], [x]), RemoteState(Source.NAVIDROME, [x], [])],
    )
    c2 = _conflict(
        session,
        "B",
        ours=[y],
        states=[RemoteState(Source.APPLE_XML, [], [y]), RemoteState(Source.NAVIDROME, [y], [])],
    )
    # On A: take remote 1 (apple_xml). Move next to B: keep ours. Commit.
    scripted("1", "n", "o", "c")
    out = PlaylistConflictUI(session, [c1, c2]).serve()
    assert out == {
        c1.playlist_id: Decision(Strategy.TAKE, Source.APPLE_XML),
        c2.playlist_id: Decision(Strategy.OURS),
    }


def test_bad_key_is_ignored_then_recovers(session, scripted):
    x = make_track(session, "x").id
    c = _conflict(
        session,
        "P",
        ours=[x],
        states=[RemoteState(Source.APPLE_XML, [], [x]), RemoteState(Source.NAVIDROME, [x], [])],
    )
    # '9' is out of range (only 2 remotes); 'z' unknown; then keep ours and commit.
    scripted("9", "z", "o", "c")
    out = PlaylistConflictUI(session, [c]).serve()
    assert out == {c.playlist_id: Decision(Strategy.OURS)}
