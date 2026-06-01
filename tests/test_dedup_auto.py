from datetime import UTC, datetime

from airdrome.normalize.dedup.auto import AutoDedupResult, auto_deduplicate, compute_auto_dedup_groups
from airdrome.normalize.dedup.grouping import CanonStrategy, flag_set

from factories import make_dedup_group, make_track


# --- compute_auto_dedup_groups ---


def test_compute_groups_default_flags_groups_exact_matches(session):
    # album / album_artist NULL ⇒ unique-constraint allows two identical rows
    make_track(session, "Song", "Artist")
    make_track(session, "Song", "Artist")

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
    make_track(session, "", "X")
    make_track(session, "", "X")

    assert compute_auto_dedup_groups(session) == []


def test_compute_groups_duration_bucketed_to_5s(session):
    # 248 and 251 both round to bucket 250 → group
    make_track(session, "S", "A", duration=248)
    make_track(session, "S", "A", duration=251)

    groups = compute_auto_dedup_groups(session)
    assert len(groups) == 1


def test_compute_groups_duration_buckets_separate_when_far_apart(session):
    # 247 rounds to 245, 253 rounds to 255 → different buckets, no group
    make_track(session, "S", "A", duration=247)
    make_track(session, "S", "A", duration=253)

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
        "S",
        "A",
        date_added=datetime(2024, 1, 1, tzinfo=UTC),
    )

    [g] = compute_auto_dedup_groups(session)
    assert g[0].id == early.id
    assert g[1].id == late.id


def test_canon_strategy_year_prefers_oldest_release(session):
    # A was added earlier but released later; B was added later but released earlier.
    a = make_track(session, "S", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC), year=2010)
    b = make_track(session, "S", "A", date_added=datetime(2024, 1, 1, tzinfo=UTC), year=2000)

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
    t2 = make_track(session, "Song", "Artist", date_added=datetime(2024, 1, 1, tzinfo=UTC))

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
    make_track(session, "S", "A")
    make_track(session, "Solo", "B")

    result = auto_deduplicate(session)

    assert result.auto_twins == 1  # one twin (one pair)
    assert len(result.groups) == 1
    assert result.manual_changes == 0


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


def test_auto_deduplicate_flattens_chains_after_manual_overrides(session):
    # Three identical tracks; auto will pick T1 as canon, T2 and T3 as twins
    t1 = make_track(session, "S", "A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "S", "A", date_added=datetime(2021, 1, 1, tzinfo=UTC), album="B")
    t3 = make_track(session, "S", "A", date_added=datetime(2022, 1, 1, tzinfo=UTC), album="C")

    # Manual override flips T1↔T2: T2 is the user-preferred canon, T1 is a twin.
    # This creates a chain post-overrides: T3 → T1 → T2.
    # Loosen album so all three group; the override only mentions T1 and T2.
    make_dedup_group(session, [(t1, t2), (t2, None)], label="flip")

    auto_deduplicate(session, flag_sets=[{"with_album": False}])
    for t in (t1, t2, t3):
        session.refresh(t)

    # Post-flatten: every canon_id must terminate (no canon points at a twin)
    assert t2.canon_id is None
    assert t1.canon_id == t2.id
    assert t3.canon_id == t2.id  # was T1, flattened to T2


def test_auto_deduplicate_applies_stored_manual_overrides(session):
    # Two duplicates; auto would pick T1 as canon (earlier date_added).
    # Stored override flips: T2 is canon, T1 is twin.
    t1 = make_track(session, "Song", "Artist", "Album A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "Song", "Artist", "Album B", date_added=datetime(2024, 1, 1, tzinfo=UTC))
    make_dedup_group(session, [(t1, t2), (t2, None)], label="manual-flip")

    result = auto_deduplicate(session, flag_sets=[{"with_album": False}])
    session.refresh(t1)
    session.refresh(t2)

    assert result.manual_changes >= 1
    assert t2.canon_id is None  # user's pick
    assert t1.canon_id == t2.id
