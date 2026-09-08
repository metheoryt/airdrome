"""Hard-conflict detection across remotes.

A reconcile run touches one playlist against several remotes. The merge auto-resolves
almost everything; the one case it must not silently guess is an *order-dependent* edit —
one remote added a track while another removed it (each vs. its own base), so the final
membership depends on which remote is reconciled first. These tests pin exactly which
track sets count as conflicts, and how `resolve_latest` settles them.
"""

from airdrome.enums import Source
from airdrome.playlists.conflicts import PlaylistConflict, RemoteState, detect_conflicts, resolve_latest


def _st(remote: Source, base, theirs) -> RemoteState:
    return RemoteState(remote=remote, base=base, theirs=theirs)


def _conflict(ours, states) -> PlaylistConflict:
    return PlaylistConflict(
        playlist_id=1,
        playlist_name="P",
        ours=ours,
        states=states,
        conflicts=detect_conflicts(states),
    )


def test_pure_adds_never_conflict():
    # two remotes each add a different track -> union, no conflict
    states = [
        _st(Source.APPLE_XML, base=[1], theirs=[1, 2]),
        _st(Source.NAVIDROME, base=[1], theirs=[1, 3]),
    ]
    assert detect_conflicts(states) == set()


def test_both_add_same_track_is_not_a_conflict():
    states = [
        _st(Source.APPLE_XML, base=[], theirs=[9]),
        _st(Source.NAVIDROME, base=[], theirs=[9]),
    ]
    assert detect_conflicts(states) == set()


def test_one_sided_removal_is_deterministic_not_a_conflict():
    # only Navidrome changes track 5 (removes it); Apple leaves it untouched
    states = [
        _st(Source.APPLE_XML, base=[5], theirs=[5]),
        _st(Source.NAVIDROME, base=[5], theirs=[]),
    ]
    assert detect_conflicts(states) == set()


def test_add_vs_remove_same_track_is_a_conflict():
    # Apple adds 7 (absent from its base); Navidrome removes 7 (present in its base)
    states = [
        _st(Source.APPLE_XML, base=[], theirs=[7]),
        _st(Source.NAVIDROME, base=[7], theirs=[]),
    ]
    assert detect_conflicts(states) == {7}


def test_conflict_is_per_track():
    # 7 conflicts (add vs remove); 8 is a pure add; 5 is a one-sided remove
    states = [
        _st(Source.APPLE_XML, base=[5], theirs=[7, 8]),
        _st(Source.NAVIDROME, base=[5, 7], theirs=[5]),
    ]
    assert detect_conflicts(states) == {7}


def test_multiplicity_bump_vs_drop_conflicts():
    # one remote raises 4's count (add), another drops it to zero (remove)
    states = [
        _st(Source.APPLE_XML, base=[4], theirs=[4, 4]),
        _st(Source.NAVIDROME, base=[4], theirs=[]),
    ]
    assert detect_conflicts(states) == {4}


def test_single_remote_never_conflicts():
    # nothing to disagree with
    states = [_st(Source.NAVIDROME, base=[1], theirs=[2])]
    assert detect_conflicts(states) == set()


# ── automatic resolution: the last remote that edited the track wins ────────


def test_clean_playlist_is_a_plain_fold_in_reconcile_order():
    # no conflict: both adds survive, folded ours -> apple -> navidrome
    apple = _st(Source.APPLE_XML, base=[1], theirs=[1, 2])
    navi = _st(Source.NAVIDROME, base=[1], theirs=[1, 3])
    c = _conflict(ours=[1], states=[apple, navi])
    assert c.conflicts == set()
    assert resolve_latest(c) == [1, 2, 3]


def test_last_editor_wins_a_removal():
    # Apple adds 7; Navidrome, reconciled later, removes it
    apple = _st(Source.APPLE_XML, base=[], theirs=[7])
    navi = _st(Source.NAVIDROME, base=[7], theirs=[])
    c = _conflict(ours=[7], states=[apple, navi])
    assert c.conflicts == {7}
    assert resolve_latest(c) == []


def test_last_editor_wins_an_addition():
    # same disagreement, opposite order: whoever edits last decides
    navi = _st(Source.NAVIDROME, base=[7], theirs=[])
    apple = _st(Source.APPLE_XML, base=[], theirs=[7])
    c = _conflict(ours=[7], states=[navi, apple])
    assert c.conflicts == {7}
    assert resolve_latest(c) == [7]


def test_verdict_overrides_the_fold_arithmetic():
    # Apple bumps 4 to two copies, Navidrome drops it. The fold alone leaves one
    # copy (2 + 0 - 1); the last editor said zero, and the verdict is what counts.
    apple = _st(Source.APPLE_XML, base=[4], theirs=[4, 4])
    navi = _st(Source.NAVIDROME, base=[4], theirs=[])
    c = _conflict(ours=[4], states=[apple, navi])
    assert c.conflicts == {4}
    assert resolve_latest(c) == []


def test_untouched_remote_abstains_instead_of_winning_by_position():
    # Spotify reconciles last but never edited 4, so Navidrome's zero stands
    apple = _st(Source.APPLE_XML, base=[4], theirs=[4, 4])
    navi = _st(Source.NAVIDROME, base=[4], theirs=[])
    spot = _st(Source.SPOTIFY, base=[4], theirs=[4])
    c = _conflict(ours=[4], states=[apple, navi, spot])
    assert c.conflicts == {4}
    assert resolve_latest(c) == []


def test_non_conflicting_edits_survive_the_override():
    # 7 conflicts; 8 (Apple) and 9 (Navidrome) are pure adds and must both stay
    apple = _st(Source.APPLE_XML, base=[], theirs=[7, 8])
    navi = _st(Source.NAVIDROME, base=[7], theirs=[9])
    c = _conflict(ours=[7], states=[apple, navi])
    assert c.conflicts == {7}
    assert resolve_latest(c) == [8, 9]


def test_verdict_carries_multiplicity_exactly():
    # the last editor wants two copies; the fold alone would have left one
    apple = _st(Source.APPLE_XML, base=[4], theirs=[])
    navi = _st(Source.NAVIDROME, base=[4], theirs=[4, 4])
    c = _conflict(ours=[4], states=[apple, navi])
    assert c.conflicts == {4}
    assert resolve_latest(c) == [4, 4]


def test_resolution_is_idempotent():
    # feeding the result back as `ours` with every base re-based changes nothing
    apple = _st(Source.APPLE_XML, base=[], theirs=[7, 8])
    navi = _st(Source.NAVIDROME, base=[7], theirs=[9])
    first = resolve_latest(_conflict(ours=[7], states=[apple, navi]))
    settled = [
        _st(Source.APPLE_XML, base=list(apple.theirs), theirs=list(apple.theirs)),
        _st(Source.NAVIDROME, base=list(navi.theirs), theirs=list(navi.theirs)),
    ]
    assert resolve_latest(_conflict(ours=first, states=settled)) == first
