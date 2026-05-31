from datetime import UTC, datetime

from airdrome.normalize.dedup.grouping import merge_overlapping_groups

from factories import make_track


def test_empty_input_returns_empty(session):
    assert merge_overlapping_groups(session, []) == []


def test_single_group_passes_through_unchanged(session):
    a = make_track(session, "A")
    b = make_track(session, "B")
    original = [a, b]

    [(key, tracks)] = merge_overlapping_groups(session, [("g0", original)])

    assert key == "g0"
    # single-component shortcut preserves the original list object
    assert tracks is original


def test_disjoint_groups_not_merged(session):
    a = make_track(session, "A")
    b = make_track(session, "B")
    c = make_track(session, "C")
    d = make_track(session, "D")

    merged = merge_overlapping_groups(session, [("g0", [a, b]), ("g1", [c, d])])

    assert len(merged) == 2
    keys = {key for key, _ in merged}
    assert keys == {"g0", "g1"}


def test_two_groups_sharing_one_track_merge(session):
    a = make_track(session, "A")
    shared = make_track(session, "S")
    b = make_track(session, "B")

    [(key, tracks)] = merge_overlapping_groups(session, [("g0", [a, shared]), ("g1", [shared, b])])

    # key joins component members with " + " and sorts
    assert key == "g0 + g1"
    assert {t.id for t in tracks} == {a.id, shared.id, b.id}


def test_three_groups_chain_via_shared_track(session):
    a = make_track(session, "A")
    s1 = make_track(session, "S1")
    s2 = make_track(session, "S2")
    b = make_track(session, "B")

    [(key, tracks)] = merge_overlapping_groups(
        session,
        [("g0", [a, s1]), ("g1", [s1, s2]), ("g2", [s2, b])],
    )

    assert key == "g0 + g1 + g2"
    assert {t.id for t in tracks} == {a.id, s1.id, s2.id, b.id}


def test_merged_component_reordered_by_canon_priority(session):
    # earliest date_added wins; then earliest year; then loved=True; then lowest id
    early = make_track(session, "early", artist="x", date_added=datetime(2020, 1, 1, tzinfo=UTC), year=2020)
    late = make_track(session, "late", artist="x", date_added=datetime(2024, 1, 1, tzinfo=UTC), year=2024)
    shared = make_track(session, "shared", artist="x", date_added=datetime(2022, 1, 1, tzinfo=UTC), year=2022)

    [(_, tracks)] = merge_overlapping_groups(
        session,
        [("g0", [late, shared]), ("g1", [shared, early])],
    )

    assert [t.id for t in tracks] == [early.id, shared.id, late.id]


def test_skips_tracks_with_none_id(session):
    a = make_track(session, "A")
    b = make_track(session, "B")

    from airdrome.models import Track

    unflushed = Track(title="ghost")  # not added/flushed — id is None
    # union-find must not crash on the None-id track; it's silently skipped
    merged = merge_overlapping_groups(session, [("g0", [a, unflushed]), ("g1", [b, unflushed])])

    # since `unflushed` doesn't participate in the union-find, g0 and g1 stay disjoint
    assert len(merged) == 2
