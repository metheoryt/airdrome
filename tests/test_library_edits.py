"""Tests for the same/different track point edits, stored as pair decisions and applied by replanning."""

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from airdrome.enums import BindMethod, DecisionKind, DecisionOrigin, Source
from airdrome.library.edits import (
    EditError,
    bind_aliases,
    group_of,
    mark_different,
    mark_same,
    reject_aliases,
)
from airdrome.models import (
    CanonChoice,
    PairDecision,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
)
from airdrome.normalize.dedup.auto import auto_deduplicate, edit_scope, plan_dedup
from airdrome.normalize.dedup.decisions import key_text, pair, track_key
from airdrome.normalize.dedup.grouping import RECOMMENDED_SETS, CanonStrategy, flag_set
from airdrome.normalize.dedup.store import MIRROR_SNAPSHOT_KEY, import_decisions
from airdrome.scrobbles.copy_plays import do_copy_plays
from airdrome.scrobbles.match_aliases import do_match_aliases

from factories import add_pair, make_track


SAME, DIFF = DecisionKind.SAME, DecisionKind.DIFFERENT
MCP, IMPORT = DecisionOrigin.MCP, DecisionOrigin.IMPORT
REC_X = "11111111-1111-1111-1111-111111111111"


def _at(day: int) -> datetime:
    """A fixed tz-aware UTC date, so `canon_order` (date_added first) is explicit in each test."""
    return datetime(2020, 1, day, tzinfo=UTC)


def _decided(n: int) -> datetime:
    """The n-th fixture decision time (all long before any edit's "now")."""
    return datetime(2021, 1, 1, tzinfo=UTC) + timedelta(hours=n)


def _link(s, twin: Track, canon: Track) -> None:
    """Make `twin` an auto-linked twin of `canon`."""
    twin.canon = canon
    twin.canon_method = BindMethod.DEDUP_FLAGS
    s.flush()


def _fp(seed: int, n: int = 120) -> list[int]:
    """A deterministic fingerprint with values spread enough to fill the index's coarse buckets."""
    rng = random.Random(seed)
    return [rng.getrandbits(31) for _ in range(n)]


def _add_file(
    s,
    track: Track,
    name: str,
    bitrate: int,
    is_main: bool = False,
    recording: str | None = None,
    fp: int | None = None,
) -> TrackFile:
    """Attach one file to `track` (with the fingerprint of seed `fp`, if given)."""
    tf = TrackFile(
        source_path=Path(f"/src/{name}.mp3"),
        track_id=track.id,
        bitrate=bitrate,
        is_main=is_main,
        recording_mbid=recording,
        fingerprint=_fp(fp) if fp is not None else None,
        fp_duration=200.0 if fp is not None else None,
    )
    s.add(tf)
    s.flush()
    return tf


def _dup(s, title: str, album: str, day: int) -> Track:
    """A track the recommended flag sets group with every other `_dup` of the same title."""
    return make_track(s, title, "Band", album=album, duration=200, date_added=_at(day))


def _root(t: Track) -> int:
    """The id of the root of `t`'s group (one hop: canon_id is terminal)."""
    return t.canon_id or t.id


def _n_pairs(s) -> int:
    """Number of stored pair decisions."""
    return s.scalar(select(func.count()).select_from(PairDecision))


def test_group_of_lists_canon_first(session):
    a = make_track(session, "A", date_added=_at(1))
    a2 = make_track(session, "A2", date_added=_at(2))
    _link(session, a2, a)

    assert [t.id for t in group_of(session, a2.id)] == [a.id, a2.id]
    assert [t.id for t in group_of(session, a.id)] == [a.id, a2.id]


def test_group_of_unknown_id(session):
    with pytest.raises(EditError):
        group_of(session, 999_999)


def test_same_merges_two_groups(session):
    a = _dup(session, "Song", "X", 1)
    a2 = _dup(session, "Song", "Y", 2)
    b = make_track(session, "B", date_added=_at(3))
    auto_deduplicate(session, RECOMMENDED_SETS)
    assert a2.canon_id == a.id

    canon = mark_same(session, a.id, b.id)

    assert canon.id == a.id
    assert a.canon_id is None
    assert (a2.canon_id, a2.canon_method, a2.canon_folded) == (a.id, BindMethod.DEDUP_FLAGS, False)
    assert (b.canon_id, b.canon_method, b.canon_confidence) == (a.id, BindMethod.MANUAL, 1.0)
    assert b.canon_folded  # joined only by the manual decision: outside a's core
    assert (a.canon_method, a.canon_confidence) == (None, None)
    assert MIRROR_SNAPSHOT_KEY in session.info


def test_same_writes_one_row(session):
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))

    mark_same(session, a.id, b.id)

    row = session.scalars(select(PairDecision)).one()
    assert (row.kind, row.origin.value) == (SAME, "mcp")
    assert session.scalar(select(func.count()).select_from(CanonChoice)) == 0


def test_same_is_idempotent(session):
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))

    mark_same(session, a.id, b.id)
    mark_same(session, a.id, b.id)

    assert _n_pairs(session) == 1
    assert a.canon_id is None
    assert b.canon_id == a.id


def test_same_rejects_self(session):
    a = make_track(session, "A")
    with pytest.raises(EditError):
        mark_same(session, a.id, a.id)


def test_same_rejects_unknown_id(session):
    a = make_track(session, "A")
    with pytest.raises(EditError):
        mark_same(session, a.id, 999_999)


def test_same_recomputes_main(session):
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    fa = _add_file(session, a, "a", 192_000, is_main=True)
    fb = _add_file(session, b, "b", 320_000, is_main=True)

    mark_same(session, a.id, b.id)

    assert fa.is_main != fb.is_main  # one main per group


def test_different_keeps_one_main_in_both_halves(session):
    a = _dup(session, "Song", "X", 1)
    b = _dup(session, "Song", "Y", 2)
    fa = _add_file(session, a, "a", 192_000)
    fb = _add_file(session, b, "b", 320_000)
    auto_deduplicate(session, RECOMMENDED_SETS)
    assert (b.canon_id, fa.is_main, fb.is_main) == (a.id, False, True)

    mark_different(session, a.id, b.id)

    # A's group lost B and its main file, so A's own file must become main.
    assert (a.canon_id, b.canon_id) == (None, None)
    assert (fa.is_main, fb.is_main) == (True, True)


def test_edit_leaves_unrelated_groups_main_alone(session):
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    # An unrelated group whose main is "wrong" by the selection rule (the better copy is not main).
    u = make_track(session, "U", date_added=_at(3))
    fu_lo = _add_file(session, u, "u-lo", 128_000, is_main=True)
    fu_hi = _add_file(session, u, "u-hi", 320_000)

    mark_same(session, a.id, b.id)
    mark_different(session, a.id, b.id)

    assert (fu_lo.is_main, fu_hi.is_main) == (True, False)


def test_a_later_edit_on_a_pair_wins(session):
    """same then different on one pair, even within one clock tick: the later edit is the stored one."""
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))

    mark_same(session, a.id, b.id)
    mark_different(session, a.id, b.id)

    assert session.scalars(select(PairDecision)).one().kind == DIFF
    assert (a.canon_id, b.canon_id) == (None, None)


def test_edit_beats_an_imported_future_dated_pair(session):
    """A file from a fast clock is clamped at import, so the owner's edit wins and is not in the future."""
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    future = datetime(2100, 1, 1, tzinfo=UTC)
    import_decisions(
        session, [pair(key_text(track_key(a)), key_text(track_key(b)), SAME, future, IMPORT)], []
    )

    mark_different(session, a.id, b.id)

    row = session.scalars(select(PairDecision)).one()
    assert (row.kind, row.origin) == (DIFF, MCP)
    assert row.decided_at <= datetime.now(UTC)
    assert (a.canon_id, b.canon_id) == (None, None)


def test_edit_beats_a_stored_future_dated_pair(session):
    """A row already stored with a future time (written before clamping) still loses to the edit."""
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    future = datetime(2100, 1, 1, tzinfo=UTC)
    add_pair(session, a, b, SAME, at=future)

    mark_different(session, a.id, b.id)

    row = session.scalars(select(PairDecision)).one()
    assert (row.kind, row.decided_at) == (DIFF, future + timedelta(microseconds=1))
    assert (a.canon_id, b.canon_id) == (None, None)


def test_edit_extends_a_decided_group(session):
    w = make_track(session, "W", date_added=_at(1))
    x = make_track(session, "X", date_added=_at(2))
    y = make_track(session, "Y", date_added=_at(3))
    add_pair(session, w, x, SAME)
    auto_deduplicate(session, [flag_set()])

    mark_same(session, x.id, y.id)

    assert (w.canon_id, x.canon_id, y.canon_id) == (None, w.id, w.id)


def test_different_detaches_canon(session):
    a = make_track(session, "A", date_added=_at(1))
    a2 = make_track(session, "A2", date_added=_at(2))
    a3 = make_track(session, "A3", date_added=_at(3))
    add_pair(session, a, a2, SAME, at=_decided(1))
    add_pair(session, a2, a3, SAME, at=_decided(2))
    auto_deduplicate(session, RECOMMENDED_SETS)
    assert (a2.canon_id, a3.canon_id) == (a.id, a.id)

    mark_different(session, a2.id, a.id)

    assert a.canon_id is None
    assert [t.id for t in group_of(session, a.id)] == [a.id]
    # The new root is the first remaining member in canon order.
    assert a2.canon_id is None
    assert a3.canon_id == a2.id


def test_different_detaches_twin(session):
    a = make_track(session, "A", date_added=_at(1))
    a2 = make_track(session, "A2", date_added=_at(2))
    a3 = make_track(session, "A3", date_added=_at(3))
    add_pair(session, a, a2, SAME, at=_decided(1))
    add_pair(session, a, a3, SAME, at=_decided(2))
    auto_deduplicate(session, RECOMMENDED_SETS)

    mark_different(session, a.id, a3.id)

    assert a3.canon_id is None
    assert (a3.canon_method, a3.canon_confidence, a3.canon_folded) == (None, None, False)
    assert a2.canon_id == a.id
    assert a.canon_id is None


def test_different_supersedes_older_chain_member(session):
    """Root a, twins b and c via same(a,c) older than same(c,b): different(a,b) supersedes same(a,c)."""
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    c = make_track(session, "C", date_added=_at(3))
    add_pair(session, a, c, SAME, at=_decided(1))
    add_pair(session, c, b, SAME, at=_decided(2))
    auto_deduplicate(session, RECOMMENDED_SETS)
    assert (b.canon_id, c.canon_id) == (a.id, a.id)

    mark_different(session, a.id, b.id)

    assert [t.id for t in group_of(session, a.id)] == [a.id]
    assert _root(b) == _root(c) != a.id


def test_edit_reaches_a_track_its_decision_drops_from_a_group(session):
    """x was a's twin through same(x,a); the new same(a,b) supersedes it (different(x,b) is newer).

    x is no seed and sits in no group of the new plan: only the current-group closure reaches it.
    """
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    x = make_track(session, "X", date_added=_at(3))
    add_pair(session, x, a, SAME, at=_decided(1))
    add_pair(session, x, b, DIFF, at=_decided(2))
    auto_deduplicate(session, RECOMMENDED_SETS)
    assert x.canon_id == a.id

    mark_same(session, a.id, b.id)

    assert (a.canon_id, b.canon_id, x.canon_id) == (None, a.id, None)
    assert (x.canon_method, x.canon_folded) == (None, False)


def test_different_rejects_self(session):
    a = make_track(session, "A")
    with pytest.raises(EditError):
        mark_different(session, a.id, a.id)


def test_different_survives_dedup(session):
    # Same title, artist and duration; only the album differs, and the flag-set below ignores the
    # album, so dedup would pair them.
    a = make_track(session, "Song", "Artist", album="X", duration=200, date_added=_at(1))
    b = make_track(session, "Song", "Artist", album="Y", duration=200, date_added=_at(2))
    c = make_track(session, "Other", "Artist", album="X", duration=100, date_added=_at(3))
    d = make_track(session, "Other", "Artist", album="Y", duration=100, date_added=_at(4))
    sets = [flag_set("artist", "duration")]

    mark_different(session, a.id, b.id)
    auto_deduplicate(session, sets)

    assert c.canon_id is None
    assert d.canon_id == c.id  # control: the unedited pair is grouped by the same flag-set
    assert a.canon_id is None
    assert b.canon_id is None


def test_different_splits_then_survives_dedup(session):
    a = make_track(session, "Song", "Artist", album="X", duration=200, date_added=_at(1))
    b = make_track(session, "Song", "Artist", album="Y", duration=200, date_added=_at(2))
    sets = [flag_set("artist", "duration")]
    auto_deduplicate(session, sets)
    assert b.canon_id == a.id

    mark_different(session, a.id, b.id)
    assert (a.canon_id, b.canon_id) == (None, None)
    auto_deduplicate(session, sets)

    assert (a.canon_id, b.canon_id) == (None, None)


def test_same_survives_dedup(session):
    a = make_track(session, "Alpha", "Artist", date_added=_at(1))
    b = make_track(session, "Beta", "Other", date_added=_at(2))

    mark_same(session, a.id, b.id)
    auto_deduplicate(session)

    assert a.canon_id is None
    assert b.canon_id == a.id
    assert b.canon_method == BindMethod.MANUAL


def test_same_may_override_its_own_veto(session):
    a = make_track(session, "A", date_added=_at(1))
    b = make_track(session, "B", date_added=_at(2))
    mark_different(session, a.id, b.id)

    mark_same(session, a.id, b.id)

    assert b.canon_id == a.id
    assert session.scalars(select(PairDecision)).one().kind == SAME


# --- an edit equals a full dedup ---


def _library(s) -> dict[str, Track]:
    """A small library with a recording group, flag-set groups, older decisions and an unrelated group.

    Every track but the fingerprint trio has one main file, as `organize` leaves them; bitrates differ so
    the main pick matters. The trio: P holds two fingerprinted files (FP1 at 128k with the lower id, FP2
    at 256k), Q matches FP2 (192k), R matches FP1 (320k). Which of P's files is main depends on its group,
    so a representative fingerprint read from `is_main` would make the grouping depend on the last run.
    """
    t = {
        "r1": make_track(s, "Rec One", "Band A", date_added=_at(1)),
        "r2": make_track(s, "Rec Two", "Band B", date_added=_at(2)),
        "f1": _dup(s, "Song", "P", 3),
        "f2": _dup(s, "Song", "Q", 4),
        "f3": _dup(s, "Song", "R", 5),
        "o1": make_track(s, "Odd", "Loner", date_added=_at(6)),
        "o2": make_track(s, "Odder", "Loner", date_added=_at(7)),
        "u1": _dup(s, "Unrelated", "P", 8),
        "u2": _dup(s, "Unrelated", "Q", 9),
    }
    for i, (name, track) in enumerate(t.items()):
        recording = REC_X if name in ("r1", "r2") else None
        _add_file(s, track, name, 128_000 + 32_000 * ((i * 5) % 7), is_main=True, recording=recording)
    add_pair(s, t["o1"], t["f2"], SAME, at=_decided(1))
    add_pair(s, t["o2"], t["o1"], DIFF, at=_decided(2))
    t |= _fingerprint_trio(s)
    return t


def _fingerprint_trio(s) -> dict[str, Track]:
    """P (two fingerprinted files), Q matching P's better file, R matching P's lower-id file."""
    trio = {
        "p": make_track(s, "Print P", "Fp Band P", date_added=_at(10)),
        "q": make_track(s, "Print Q", "Fp Band Q", date_added=_at(11)),
        "r": make_track(s, "Print R", "Fp Band R", date_added=_at(12)),
    }
    _add_file(s, trio["p"], "p-fp1", 128_000, fp=1)
    _add_file(s, trio["p"], "p-fp2", 256_000, fp=2)
    _add_file(s, trio["q"], "q-fp2", 192_000, fp=2)
    _add_file(s, trio["r"], "r-fp1", 320_000, fp=1)
    return trio


def _state(s, ids: set[int]) -> dict:
    """The canon link of every track in `ids` and the `is_main` flag of each of their files."""
    links = {
        t.id: (t.canon_id, t.canon_method, t.canon_confidence, t.canon_folded)
        for t in s.scalars(select(Track).where(Track.id.in_(ids)))
    }
    mains = {
        ("file", f.id): f.is_main for f in s.scalars(select(TrackFile).where(TrackFile.track_id.in_(ids)))
    }
    return links | mains


EDITS = [
    (mark_same, "f1", "r2"),
    (mark_different, "f1", "o1"),
    (mark_same, "o2", "u1"),
    (mark_different, "f2", "f3"),
    (mark_different, "p", "r"),  # the edit moves P's main file
]


@pytest.mark.parametrize(("edit", "x", "y"), EDITS)
def test_edit_equals_full_dedup(session, edit, x, y):
    t = _library(session)
    auto_deduplicate(session, RECOMMENDED_SETS)
    every = {tr.id for tr in t.values()}
    before = _state(session, every)

    edit(session, t[x].id, t[y].id)
    session.expire_all()
    edited = _state(session, every)
    scope = edit_scope(
        session, plan_dedup(session, RECOMMENDED_SETS, CanonStrategy.ADDED), {t[x].id, t[y].id}
    )
    auto_deduplicate(session, RECOMMENDED_SETS)
    session.expire_all()

    assert _state(session, scope) == _state_from(edited, session, scope)
    outside = every - scope
    assert outside
    assert _state_from(edited, session, outside) == _state_from(before, session, outside)


def _state_from(state: dict, s, ids: set[int]) -> dict:
    """The part of a recorded `_state` that belongs to the tracks in `ids` (their links and files)."""
    files = {("file", f.id) for f in s.scalars(select(TrackFile).where(TrackFile.track_id.in_(ids)))}
    return {k: v for k, v in state.items() if k in ids or k in files}


def test_dedup_twice_gives_the_same_canons(session):
    """`dedup` is idempotent even where the main file moves: P's representative fingerprint is fixed.

    P's FP2 file starts as main, joining P to Q; Q's file is better, so P loses the main, and a
    representative read from `is_main` would then fall back to P's lower-id FP1 file and switch P to R
    on the next run.
    """
    trio = _fingerprint_trio(session)
    for f in trio["p"].files:
        f.is_main = f.bitrate == 256_000
    trio["q"].files[0].bitrate = 320_000  # better than P's FP2 file: Q takes the main
    trio["r"].files[0].bitrate = 128_000
    session.flush()

    auto_deduplicate(session, RECOMMENDED_SETS)
    first = {t.id: t.canon_id for t in session.scalars(select(Track))}
    auto_deduplicate(session, RECOMMENDED_SETS)

    assert {t.id: t.canon_id for t in session.scalars(select(Track))} == first
    assert first[trio["q"].id] == trio["p"].id or first[trio["p"].id] == trio["q"].id


def test_edit_on_undeduped_db_matches_full_dedup(session):
    """Decisions restored after a rebuild but not yet `dedup`-ed: every canon_id is NULL."""
    t = _library(session)
    every = {tr.id for tr in t.values()}

    mark_same(session, t["f1"].id, t["r2"].id)
    session.expire_all()
    edited = _state(session, every)
    scope = edit_scope(
        session, plan_dedup(session, RECOMMENDED_SETS, CanonStrategy.ADDED), {t["f1"].id, t["r2"].id}
    )
    auto_deduplicate(session, RECOMMENDED_SETS)
    session.expire_all()

    assert _state(session, scope) == _state_from(edited, session, scope)
    assert edited[t["r2"].id][0] is not None  # the edit linked something
    # Outside the scope the edit promises only to leave the tracks alone: still unlinked.
    outside = every - scope
    assert outside
    assert all(edited[i] == (None, None, None, False) for i in outside)


def _alias(s, title: str, artist: str, n: int = 3) -> TrackAlias:
    """An alias with `n` scrobbles on distinct days."""
    a = TrackAlias(title=title, artist=artist, album="")
    s.add(a)
    s.flush()
    for day in range(1, n + 1):
        s.add(TrackAliasScrobble(alias_id=a.id, date=_at(day), platform=Source.LASTFM))
    s.flush()
    s.refresh(a)
    return a


def _plays(s, track: Track) -> int:
    """Number of `TrackPlay` rows on `track`."""
    return s.scalar(select(func.count()).select_from(TrackPlay).where(TrackPlay.track_id == track.id))


def _fuzzy_bound(s) -> tuple[Track, Track, TrackAlias]:
    """Track X, an unrelated track Y, and an alias fuzzy-bound to X with its plays copied."""
    x = make_track(s, "Song", "Artist")
    y = make_track(s, "Other", "Someone")
    alias = _alias(s, "Song", "Artist")
    do_match_aliases(s)
    do_copy_plays(s)
    assert alias.track_id == x.id
    return x, y, alias


def test_bind_moves_plays(session):
    x, y, alias = _fuzzy_bound(session)
    assert _plays(session, x) == 3

    result = bind_aliases(session, [alias.id], y.id)

    assert result.id == y.id
    assert (_plays(session, x), _plays(session, y)) == (0, 3)
    assert alias.track_id == y.id
    assert alias.match_method == BindMethod.MANUAL
    assert alias.match_confidence == 1.0


def test_bind_resolves_to_canon(session):
    canon = make_track(session, "C", date_added=_at(1))
    twin = make_track(session, "T", date_added=_at(2))
    _link(session, twin, canon)
    alias = _alias(session, "Zzz", "Qqq")

    assert bind_aliases(session, [alias.id], twin.id).id == canon.id
    assert alias.track_id == canon.id
    assert _plays(session, canon) == 3


def test_bind_is_idempotent(session):
    _, y, alias = _fuzzy_bound(session)

    bind_aliases(session, [alias.id], y.id)
    bind_aliases(session, [alias.id], y.id)

    assert _plays(session, y) == 3


def test_reject_removes_plays_and_survives_land(session):
    x, _, alias = _fuzzy_bound(session)
    sids = [sc.id for sc in alias.scrobbles]

    reject_aliases(session, [alias.id])

    assert alias.track_id is None
    assert alias.match_method == BindMethod.MANUAL
    assert alias.match_confidence is None
    remaining = lambda: session.scalar(  # noqa: E731
        select(func.count()).select_from(TrackPlay).where(TrackPlay.source_scrobble_id.in_(sids))
    )
    assert remaining() == 0
    do_match_aliases(session)
    do_copy_plays(session)
    assert alias.track_id is None
    assert remaining() == 0
    assert _plays(session, x) == 0


def test_manual_bind_survives_match(session):
    _, y, alias = _fuzzy_bound(session)
    bind_aliases(session, [alias.id], y.id)

    do_match_aliases(session)

    assert alias.track_id == y.id
    assert alias.match_method == BindMethod.MANUAL


def test_unknown_ids_raise(session):
    t = make_track(session, "T")
    alias = _alias(session, "A", "B")
    with pytest.raises(EditError):
        bind_aliases(session, [alias.id, 999_999], t.id)
    with pytest.raises(EditError):
        bind_aliases(session, [alias.id], 999_999)
    with pytest.raises(EditError):
        reject_aliases(session, [999_999])
    assert alias.track_id is None
