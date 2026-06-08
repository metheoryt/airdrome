"""Hard-conflict detection across remotes.

A reconcile run touches one playlist against several remotes. The merge auto-resolves
almost everything; the one case it must not silently guess is an *order-dependent* edit —
one remote added a track while another removed it (each vs. its own base), so the final
membership depends on which remote is reconciled first. These tests pin exactly which
track sets count as conflicts.
"""

from airdrome.enums import Source
from airdrome.playlists.conflicts import (
    Decision,
    PlaylistConflict,
    RemoteState,
    Strategy,
    detect_conflicts,
    resolve_final,
)


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


# ── resolution strategies ───────────────────────────────────────────────────


def test_keep_ours_returns_canonical_untouched():
    c = _conflict(ours=[1, 2], states=[_st(Source.APPLE_XML, [], [7]), _st(Source.NAVIDROME, [7], [])])
    assert resolve_final(c, Decision(Strategy.OURS)) == [1, 2]


def test_take_remote_returns_that_remotes_membership():
    apple = _st(Source.APPLE_XML, base=[], theirs=[7, 8])
    navi = _st(Source.NAVIDROME, base=[7], theirs=[])
    c = _conflict(ours=[7], states=[apple, navi])
    assert resolve_final(c, Decision(Strategy.TAKE, Source.APPLE_XML)) == [7, 8]
    assert resolve_final(c, Decision(Strategy.TAKE, Source.NAVIDROME)) == []


def test_take_unknown_remote_raises():
    c = _conflict(ours=[1], states=[_st(Source.NAVIDROME, [1], [])])
    try:
        resolve_final(c, Decision(Strategy.TAKE, Source.SPOTIFY))
    except ValueError:
        return
    raise AssertionError("expected ValueError for a remote not in the conflict")


def test_auto_folds_remotes_in_order():
    # Apple adds 7, Navidrome removes 7: AUTO folds ours->apple->navidrome.
    # ours[] -> apple union -> [7] -> navidrome (base[7],ours[7],theirs[]) -> []
    apple = _st(Source.APPLE_XML, base=[], theirs=[7])
    navi = _st(Source.NAVIDROME, base=[7], theirs=[])
    c = _conflict(ours=[], states=[apple, navi])
    assert resolve_final(c, Decision(Strategy.AUTO)) == []
