import pytest
from sqlalchemy import select

from airdrome.models import DedupGroup
from airdrome.normalize.dedup.persistence import (
    apply_manual_overrides,
    flatten_canon_chains,
    load_confirmed_groups,
    save_confirmed_groups,
)

from factories import make_dedup_group, make_page, make_track


# --- save_confirmed_groups ---


def test_save_inserts_new_group_when_confirmed(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    page = make_page([t1, t2])
    page.chosen_canons = [None, t1.id]
    page.confirmed = True

    save_confirmed_groups(session, {"k": page})

    [g] = session.scalars(select(DedupGroup)).all()
    assert g.label == "k"
    by_hash = {m.member_hash: m.canon_hash for m in g.members}
    assert by_hash[t1.duplicate_hash] is None
    assert by_hash[t2.duplicate_hash] == t1.duplicate_hash


def test_save_updates_existing_group_on_match(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    # pre-existing group with t2 as canon
    make_dedup_group(session, [(t1, t2), (t2, None)], label="old")

    page = make_page([t1, t2])
    page.chosen_canons = [None, t1.id]  # flip — t1 is now canon
    page.confirmed = True

    save_confirmed_groups(session, {"new": page})

    groups = session.scalars(select(DedupGroup)).all()
    assert len(groups) == 1  # updated, not duplicated
    [g] = groups
    assert g.label == "new"
    by_hash = {m.member_hash: m.canon_hash for m in g.members}
    assert by_hash[t1.duplicate_hash] is None
    assert by_hash[t2.duplicate_hash] == t1.duplicate_hash


def test_save_deletes_existing_when_page_no_longer_confirmed(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="old")

    page = make_page([t1, t2])
    page.confirmed = False  # user reset their pick

    save_confirmed_groups(session, {"k": page})

    assert session.scalars(select(DedupGroup)).all() == []


def test_save_skips_unconfirmed_page_with_no_existing_row(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    page = make_page([t1, t2])
    page.confirmed = False

    save_confirmed_groups(session, {"k": page})

    assert session.scalars(select(DedupGroup)).all() == []


def test_save_does_not_touch_stored_groups_outside_this_run(session):
    a1 = make_track(session, "OnlyA1", "ArtA")
    a2 = make_track(session, "OnlyA2", "ArtA")
    make_dedup_group(session, [(a1, None), (a2, a1)], label="untouched")

    b1 = make_track(session, "OnlyB1", "ArtB")
    b2 = make_track(session, "OnlyB2", "ArtB")
    page = make_page([b1, b2])
    page.chosen_canons = [None, b1.id]
    page.confirmed = True

    save_confirmed_groups(session, {"new": page})

    groups = session.scalars(select(DedupGroup)).all()
    assert {g.label for g in groups} == {"untouched", "new"}


def test_save_preserves_all_null_canons_for_reviewed_distinct(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    page = make_page([t1, t2])
    page.chosen_canons = [None, None]  # reviewed → distinct
    page.confirmed = True

    save_confirmed_groups(session, {"reviewed": page})

    [g] = session.scalars(select(DedupGroup)).all()
    assert all(m.canon_hash is None for m in g.members)


# --- load_confirmed_groups ---


def test_load_empty_index_no_changes(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    page = make_page([t1, t2])

    load_confirmed_groups(session, {"k": page})

    assert page.confirmed is False
    assert page.chosen_canons == [None, None]


def test_load_marks_matching_page_confirmed_and_restores_canons(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="g")

    page = make_page([t1, t2])
    load_confirmed_groups(session, {"k": page})

    assert page.confirmed is True
    assert page.auto_resolved is False
    assert page.chosen_canons == [None, t1.id]


def test_load_skips_when_canon_hash_unresolvable_on_page(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    ghost = make_track(session, "Ghost", "Artist", "Album G")
    # stored group's members are t1, t2 — but canon points to a track not on the page
    make_dedup_group(session, [(t1, ghost), (t2, ghost)], label="bad")

    page = make_page([t1, t2])
    load_confirmed_groups(session, {"k": page})

    assert page.confirmed is False  # skipped due to unresolvable canon_hash


def test_load_handles_member_order_differences(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="g")

    # page tracks in reverse order from stored member rows
    page = make_page([t2, t1])
    load_confirmed_groups(session, {"k": page})

    assert page.confirmed is True
    # chosen_canons aligns to page.tracks order: t2 → t1.id; t1 → None
    assert page.chosen_canons == [t1.id, None]


# --- apply_manual_overrides ---


def test_overrides_noop_when_no_stored_groups(session):
    make_track(session, "x", "y")
    assert apply_manual_overrides(session) == 0


def test_overrides_applied_to_matching_group(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="manual-test")

    changed = apply_manual_overrides(session)

    assert changed == 1
    session.refresh(t2)
    assert t2.canon_id == t1.id
    session.refresh(t1)
    assert t1.canon_id is None


def test_overrides_skips_partial_group(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="g")

    session.delete(t2)
    session.flush()

    changed = apply_manual_overrides(session)

    assert changed == 0
    session.refresh(t1)
    assert t1.canon_id is None


def test_overrides_all_null_canons_clears_canon_id(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    t2.canon_id = t1.id  # pretend auto-dedup set this
    session.flush()

    make_dedup_group(session, [(t1, None), (t2, None)], label="reviewed-distinct")

    changed = apply_manual_overrides(session)

    assert changed == 1
    session.refresh(t2)
    assert t2.canon_id is None


def test_overrides_canon_hash_missing_member_skipped(session):
    # Defensive: a stored canon_hash that doesn't correspond to any track
    # in the library (data corruption / stale hash). Should be skipped.
    from airdrome.models import DedupGroup, DedupGroupMember

    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    group = DedupGroup(label="bad")
    group.members = [
        DedupGroupMember(member_hash=t1.duplicate_hash, canon_hash="ghost-hash"),
        DedupGroupMember(member_hash=t2.duplicate_hash, canon_hash="ghost-hash"),
    ]
    session.add(group)
    session.flush()

    changed = apply_manual_overrides(session)

    assert changed == 0
    session.refresh(t1)
    session.refresh(t2)
    assert t1.canon_id is None
    assert t2.canon_id is None


def test_overrides_overrides_existing_canon_id(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    # auto-dedup picked t2 as canon
    t1.canon_id = t2.id
    session.flush()

    # user picks the opposite: t1 canon, t2 twin
    make_dedup_group(session, [(t1, None), (t2, t1)], label="g")

    changed = apply_manual_overrides(session)

    assert changed == 2  # t1 cleared, t2 set
    session.refresh(t1)
    session.refresh(t2)
    assert t1.canon_id is None
    assert t2.canon_id == t1.id


# --- flatten_canon_chains ---


def test_flatten_collapses_chain(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id  # chain A <- B <- C
    session.flush()

    changed = flatten_canon_chains(session)

    assert changed == 1
    session.refresh(c)
    assert c.canon_id == a.id


def test_flatten_idempotent(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id
    session.flush()

    flatten_canon_chains(session)
    assert flatten_canon_chains(session) == 0


def test_flatten_no_chains_returns_zero(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id  # flat: canon is terminal
    session.flush()

    assert flatten_canon_chains(session) == 0


def test_flatten_cycle_asserts(session):
    # A cycle (A↔B) is structurally invalid; the function's `seen` guard
    # prevents an infinite walk, but the post-condition assert must fire
    # because the chain can't be flattened to a terminal canon.
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    a.canon_id = b.id
    b.canon_id = a.id
    session.flush()

    with pytest.raises(AssertionError):
        flatten_canon_chains(session)


def test_flatten_multiple_disconnected_chains(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    x = make_track(session, "X1", "Y")
    y = make_track(session, "Y1", "Y")
    z = make_track(session, "Z1", "Y")
    b.canon_id = a.id
    c.canon_id = b.id
    y.canon_id = x.id
    z.canon_id = y.id
    session.flush()

    changed = flatten_canon_chains(session)

    assert changed == 2
    session.refresh(c)
    session.refresh(z)
    assert c.canon_id == a.id
    assert z.canon_id == x.id
