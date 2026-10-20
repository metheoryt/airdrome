from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from airdrome.enums import BindMethod, DecisionKind
from airdrome.models import PairDecision, Track, TrackFile
from airdrome.normalize.dedup.auto import AutoDedupResult, auto_deduplicate, compute_auto_dedup_groups
from airdrome.normalize.dedup.grouping import CanonStrategy, flag_set

from factories import add_canon, add_pair, make_track


SAME, DIFF = DecisionKind.SAME, DecisionKind.DIFFERENT
T0 = datetime(2021, 1, 1, tzinfo=UTC)


def _t(n: int) -> datetime:
    """The n-th decision time after T0, so a test names its newest-first order explicitly."""
    return T0 + timedelta(hours=n)


def _root(t: Track) -> int:
    """The id of the root of `t`'s group (one hop: canon_id is terminal)."""
    return t.canon_id or t.id


# --- compute_auto_dedup_groups ---


def test_compute_groups_default_flags_groups_exact_matches(session):
    # The key is NULLS NOT DISTINCT, so the raw keys differ in case; the _norm keys are equal.
    make_track(session, "Song", "Artist")
    make_track(session, "song", "artist")

    groups = compute_auto_dedup_groups(session)

    assert len(groups) == 1
    [g] = groups
    assert len(g) == 2


def test_compute_groups_title_only_loosens_all_other_fields(session):
    # all flags False ⇒ key is only title_norm
    make_track(session, "Same", "A1", album="X")
    make_track(session, "Same", "A2", album="Y")
    make_track(session, "Different", "A3")

    groups = compute_auto_dedup_groups(
        session,
        with_artist=False,
        with_album_artist=False,
        with_album=False,
        with_year=False,
        with_track_n=False,
        with_disc_n=False,
        with_duration=False,
    )

    assert len(groups) == 1
    [g] = groups
    assert {t.title for t in g} == {"Same"}


def test_compute_groups_excludes_singletons(session):
    make_track(session, "Solo", "A")

    assert compute_auto_dedup_groups(session) == []


def test_compute_groups_skips_blank_active_key_field(session):
    # A single-field {album} set must not collapse same-title tracks that both
    # lack an album into one bogus group keyed by (title, blank).
    make_track(session, "S", "A")  # album is None
    make_track(session, "S", "B")  # album is None

    assert compute_auto_dedup_groups(session, **flag_set("album")) == []

    # With a real (shared) album, the same set groups them.
    make_track(session, "S", "C", album="Shared")
    make_track(session, "S", "D", album="Shared")
    [g] = compute_auto_dedup_groups(session, **flag_set("album"))
    assert {t.album for t in g} == {"Shared"}


def test_compute_groups_skips_empty_title(session):
    make_track(session, "", "X")  # raw keys differ ("" vs " "); both normalize to ""
    make_track(session, " ", "X")

    assert compute_auto_dedup_groups(session) == []


def test_compute_groups_duration_bucketed_to_5s(session):
    # 248 and 251 both round to bucket 250 → group
    make_track(session, "S", "A", duration=248)
    make_track(session, "s", "a", duration=251)

    groups = compute_auto_dedup_groups(session)
    assert len(groups) == 1


def test_compute_groups_duration_buckets_separate_when_far_apart(session):
    # 247 rounds to 245, 253 rounds to 255 → different buckets, no group
    make_track(session, "S", "A", duration=247)
    make_track(session, "s", "a", duration=253)

    assert compute_auto_dedup_groups(session) == []


def test_compute_groups_sort_order_canon_first(session):
    # canon priority (ADDED): date_added asc, year asc, id asc
    early = make_track(
        session,
        "S",
        "A",
        date_added=datetime(2020, 1, 1, tzinfo=UTC),
    )
    late = make_track(
        session,
        "s",
        "a",
        date_added=datetime(2024, 1, 1, tzinfo=UTC),
    )

    [g] = compute_auto_dedup_groups(session)
    assert g[0].id == early.id
    assert g[1].id == late.id


def test_canon_strategy_year_prefers_oldest_release(session):
    # A was added earlier but released later; B was added later but released earlier.
    a = make_track(session, "S", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC), year=2010)
    b = make_track(session, "s", "a", date_added=datetime(2024, 1, 1, tzinfo=UTC), year=2000)

    # Exclude year from the bucket key so the differing years still group.
    no_year = {"with_year": False}

    # ADDED (default): earliest date_added is canon.
    [g_added] = compute_auto_dedup_groups(session, **no_year)
    assert g_added[0].id == a.id

    # YEAR: earliest release leads, overriding date_added.
    [g_year] = compute_auto_dedup_groups(session, strategy=CanonStrategy.YEAR, **no_year)
    assert g_year[0].id == b.id

    # auto_deduplicate threads the strategy through to canon_id assignment.
    auto_deduplicate(session, flag_sets=[no_year], strategy=CanonStrategy.YEAR)
    assert b.canon_id is None
    assert a.canon_id == b.id


# --- auto_deduplicate ---


def test_auto_deduplicate_writes_canon_to_twins_keeps_canon_null(session):
    t1 = make_track(session, "Song", "Artist", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "song", "artist", date_added=datetime(2024, 1, 1, tzinfo=UTC))

    result = auto_deduplicate(session)
    session.refresh(t1)
    session.refresh(t2)

    assert isinstance(result, AutoDedupResult)
    assert t1.canon_id is None  # canon by date_added priority
    assert t2.canon_id == t1.id


def test_auto_deduplicate_resets_prior_canon_ids(session):
    # Two non-duplicate tracks; one has a stale canon_id pointing at the other
    t1 = make_track(session, "Standalone1", "A")
    t2 = make_track(session, "Standalone2", "B")
    t1.canon_id = t2.id
    session.flush()

    auto_deduplicate(session)
    session.refresh(t1)
    session.refresh(t2)

    # Not duplicates → after the clean-slate reset, no canon gets re-assigned
    assert t1.canon_id is None
    assert t2.canon_id is None


def test_auto_deduplicate_returns_result_counts(session):
    make_track(session, "S", "A")
    make_track(session, "s", "a")
    make_track(session, "Solo", "B")

    result = auto_deduplicate(session)

    assert result.auto_twins == 1  # one twin (one pair)
    assert len(result.groups) == 1
    assert result.manual_links == 0


def test_auto_deduplicate_multiple_flag_sets_union_find_merges(session):
    # set0 (loosen album): T1 and T2 share artist+title
    # set1 (loosen artist): T2 and T3 share album+title
    # ⇒ {T1, T2, T3} should end up in one merged component
    t1 = make_track(
        session,
        "X",
        artist="A",
        album="P",
        date_added=datetime(2020, 1, 1, tzinfo=UTC),
    )
    t2 = make_track(
        session,
        "X",
        artist="A",
        album="Q",
        date_added=datetime(2021, 1, 1, tzinfo=UTC),
    )
    t3 = make_track(
        session,
        "X",
        artist="B",
        album="Q",
        date_added=datetime(2022, 1, 1, tzinfo=UTC),
    )

    result = auto_deduplicate(
        session,
        flag_sets=[
            {"with_album": False},
            {"with_artist": False},
        ],
    )
    for t in (t1, t2, t3):
        session.refresh(t)

    assert len(result.groups) == 1
    assert t1.canon_id is None  # earliest date_added → canon
    assert t2.canon_id == t1.id
    assert t3.canon_id == t1.id


def test_auto_deduplicate_flattens_chains_after_canon_choice(session):
    # Three identical tracks; canon order would pick T1, the canon choice picks T2.
    t1 = make_track(session, "S", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "S", "A", date_added=datetime(2021, 1, 1, tzinfo=UTC), album="B")
    t3 = make_track(session, "S", "A", date_added=datetime(2022, 1, 1, tzinfo=UTC), album="C")
    add_canon(session, t2)

    auto_deduplicate(session, flag_sets=[{"with_album": False}])

    # Every canon_id terminates on the chosen canon (no canon points at a twin).
    assert t2.canon_id is None
    assert t1.canon_id == t2.id
    assert t3.canon_id == t2.id


def test_auto_deduplicate_applies_same_and_canon_choice(session):
    # Two duplicates; canon order would pick T1 (earlier date_added). The owner's choice: T2 is canon.
    t1 = make_track(session, "Song", "Artist", "Album A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "Song", "Artist", "Album B", date_added=datetime(2024, 1, 1, tzinfo=UTC))
    add_pair(session, t1, t2, SAME)
    add_canon(session, t2)

    result = auto_deduplicate(session, flag_sets=[{"with_album": False}])

    assert result.manual_links == 1
    assert result.auto_twins == 0
    assert t2.canon_id is None
    assert t1.canon_id == t2.id
    assert (t1.canon_method, t1.canon_confidence) == (BindMethod.MANUAL, 1.0)
    assert not t1.canon_folded  # the flag-set alone joins them too: inside the core


# --- pair decisions ---


def test_repro_both_insert_orders_same_canons(session):
    """Decisions w→v and x→w (the old overlapping-groups repro) give one result in either row order."""
    v = make_track(session, "V", "Band", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    w = make_track(session, "W", "Other", date_added=datetime(2021, 1, 1, tzinfo=UTC))
    x = make_track(session, "X", "Third", date_added=datetime(2022, 1, 1, tzinfo=UTC))
    decisions = [(w, v, _t(1)), (x, w, _t(2))]

    snapshots = []
    for order in (decisions, decisions[::-1]):
        session.execute(delete(PairDecision))
        for t1, t2, at in order:
            add_pair(session, t1, t2, SAME, at=at)
        auto_deduplicate(session)
        snapshots.append({t.id: t.canon_id for t in session.scalars(select(Track))})

    assert snapshots[0] == snapshots[1]
    assert snapshots[0] == {v.id: None, w.id: v.id, x.id: v.id}


def test_newest_wins_across_transitive_chain(session):
    """same(a,b) oldest, same(b,c), then different(a,c) newest: the oldest link breaks."""
    a = make_track(session, "A", "Band", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    b = make_track(session, "B", "Other", date_added=datetime(2021, 1, 1, tzinfo=UTC))
    c = make_track(session, "C", "Third", date_added=datetime(2022, 1, 1, tzinfo=UTC))
    add_pair(session, a, b, SAME, at=_t(1))
    add_pair(session, b, c, SAME, at=_t(2))
    add_pair(session, a, c, DIFF, at=_t(3))

    result = auto_deduplicate(session)

    assert _root(b) == _root(c)
    assert _root(a) == a.id != _root(b)
    assert [t.id for t in group_of_root(session, a)] == [a.id]
    assert result.superseded == 1


def group_of_root(s, t: Track) -> list[Track]:
    """Every track whose root is `t`'s root."""
    root = _root(t)
    return [x for x in s.scalars(select(Track).order_by(Track.id)) if _root(x) == root]


def _file(s, t: Track, recording: str) -> TrackFile:
    """Attach a file carrying `recording` as its recording id."""
    f = TrackFile(
        source_path=Path(f"/src/{t.id}.mp3"), track_id=t.id, bitrate=320_000, recording_mbid=recording
    )
    s.add(f)
    s.flush()
    return f


def test_manual_same_links_across_recording_ids(session):
    a = make_track(session, "A", "Band", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    b = make_track(session, "B", "Other", date_added=datetime(2021, 1, 1, tzinfo=UTC))
    _file(session, a, "11111111-1111-1111-1111-111111111111")
    _file(session, b, "22222222-2222-2222-2222-222222222222")
    add_pair(session, a, b, SAME)

    result = auto_deduplicate(session)

    assert [[t.id for t in g] for g in result.groups] == [[a.id, b.id]]
    assert b.canon_id == a.id
    assert (b.canon_method, b.canon_confidence) == (BindMethod.MANUAL, 1.0)
    assert b.canon_folded  # only the manual decision joins it: outside a's core


def test_manual_same_beats_auto_join_to_separated_track(session):
    """same(A,B) + different(B,X), where the flag sets alone would join A and X: X stays apart."""
    a = make_track(session, "Song", "Band", "One", duration=200, date_added=datetime(2020, 1, 1, tzinfo=UTC))
    x = make_track(session, "Song", "Band", "Two", duration=200, date_added=datetime(2021, 1, 1, tzinfo=UTC))
    b = make_track(session, "Other", "Elsewhere", duration=50, date_added=datetime(2022, 1, 1, tzinfo=UTC))
    sets = [flag_set("artist", "duration")]
    auto_deduplicate(session, sets)
    assert x.canon_id == a.id  # control: the flag set joins A and X

    add_pair(session, a, b, SAME, at=_t(1))
    add_pair(session, b, x, DIFF, at=_t(2))
    auto_deduplicate(session, sets)

    assert b.canon_id == a.id
    assert b.canon_method == BindMethod.MANUAL
    assert x.canon_id is None
    assert _root(x) != _root(a)


def test_dead_decision_ignored_counted_and_kept(session):
    a = make_track(session, "A", "Band")
    gone = make_track(session, "Gone", "Band")
    add_pair(session, a, gone, SAME)
    session.delete(gone)
    session.flush()

    result = auto_deduplicate(session)

    assert result.dead == 1
    assert a.canon_id is None
    assert session.scalar(select(func.count()).select_from(PairDecision)) == 1


def test_canon_choice_wins_over_canon_order(session):
    t1 = make_track(session, "Song", "Band", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "Song", "Band", "B", date_added=datetime(2024, 1, 1, tzinfo=UTC))
    add_canon(session, t2)

    result = auto_deduplicate(session, flag_sets=[{"with_album": False}])

    assert [t.id for t in result.groups[0]] == [t2.id, t1.id]
    assert t2.canon_id is None
    assert t1.canon_id == t2.id
    assert t1.canon_method == BindMethod.DEDUP_FLAGS  # a canon choice is not a manual link


def test_newest_canon_choice_wins_in_merged_group(session):
    t1 = make_track(session, "Song", "Band", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "Song", "Band", "B", date_added=datetime(2021, 1, 1, tzinfo=UTC))
    t3 = make_track(session, "Song", "Band", "C", date_added=datetime(2022, 1, 1, tzinfo=UTC))
    add_canon(session, t3, at=_t(2))
    add_canon(session, t2, at=_t(1))

    auto_deduplicate(session, flag_sets=[{"with_album": False}])

    assert t3.canon_id is None
    assert (t1.canon_id, t2.canon_id) == (t3.id, t3.id)
