"""End-to-end scenarios that cross the auto / manual / persistence boundary."""

from datetime import UTC, datetime

from sqlalchemy import select

from airdrome.match import find_best_track
from airdrome.models import Track
from airdrome.normalize.dedup.auto import auto_deduplicate
from airdrome.normalize.dedup.manual import Deduplicator

from factories import make_dedup_group, make_track


def _canon_snapshot(session) -> dict[int, int | None]:
    return {t.id: t.canon_id for t in session.scalars(select(Track)).all()}


def test_auto_then_manual_override_layers_correctly(session):
    # Auto would pick T1 as canon (earliest date_added).
    t1 = make_track(session, "Song", "Artist", "Album A", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    t2 = make_track(session, "Song", "Artist", "Album B", date_added=datetime(2024, 1, 1, tzinfo=UTC))

    # User picks the opposite via the manual table.
    make_dedup_group(session, [(t1, t2), (t2, None)], label="flipped")

    auto_deduplicate(session, flag_sets=[{"with_album": False}])
    session.refresh(t1)
    session.refresh(t2)

    assert t2.canon_id is None  # manual wins
    assert t1.canon_id == t2.id


def test_auto_idempotent_on_repeat(session):
    make_track(session, "A", "X", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    make_track(session, "A", "X", date_added=datetime(2024, 1, 1, tzinfo=UTC), album="alt")
    make_track(session, "B", "Y")

    auto_deduplicate(session, flag_sets=[{"with_album": False}])
    snapshot1 = _canon_snapshot(session)

    auto_deduplicate(session, flag_sets=[{"with_album": False}])
    snapshot2 = _canon_snapshot(session)

    assert snapshot1 == snapshot2


def test_different_flag_sets_produce_different_groupings(session):
    # T1 and T2 share artist+title but differ on album.
    # with_album=True → not grouped. with_album=False → grouped.
    t1 = make_track(session, "S", "A", album="P")
    t2 = make_track(session, "S", "A", album="Q")

    r_strict = auto_deduplicate(session, flag_sets=[{}])  # all flags default True
    assert r_strict.auto_twins == 0

    r_loose = auto_deduplicate(session, flag_sets=[{"with_album": False}])
    session.refresh(t1)
    session.refresh(t2)
    assert r_loose.auto_twins == 1
    assert t2.canon_id == t1.id


def test_confirm_exit_reenter_restores_confirmed_state(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    make_track(session, "Song", "Artist", "Album B")

    dedup = Deduplicator(session)
    dedup.fill_state()
    pages_with_t1 = [p for p in dedup.state.pages.values() if any(t.id == t1.id for t in p.tracks)]
    page = pages_with_t1[0]
    canon_idx = next(i for i, t in enumerate(page.tracks) if t.id == t1.id)
    twin_idx = 1 - canon_idx
    page.set_canon(canon_idx, [twin_idx])
    page.confirm()

    dedup.apply_changes()
    session.flush()

    # Fresh Deduplicator should restore the confirmed pick from the DB
    fresh = Deduplicator(session)
    fresh.fill_state()
    fresh_pages_with_t1 = [p for p in fresh.state.pages.values() if any(t.id == t1.id for t in p.tracks)]
    [fresh_page] = fresh_pages_with_t1
    assert fresh_page.confirmed
    canon_by_id = dict(zip([t.id for t in fresh_page.tracks], fresh_page.chosen_canons, strict=False))
    assert canon_by_id[t1.id] is None


def test_reviewed_distinct_group_survives_rerun(session):
    # Two tracks auto would group together, but the user has reviewed them
    # and decided they're actually distinct (stored canon_hashes all None).
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, None)], label="reviewed-distinct")

    auto_deduplicate(session, flag_sets=[{"with_album": False}])
    session.refresh(t1)
    session.refresh(t2)

    # Manual override wins: neither is a twin
    assert t1.canon_id is None
    assert t2.canon_id is None


def test_empty_library_produces_no_groups_no_errors(session):
    result = auto_deduplicate(session)

    assert result.auto_twins == 0
    assert result.manual_changes == 0
    assert result.groups == []


def test_find_best_track_prefers_canonical_on_tie(session):
    canonical = make_track(session, "Same", "X")
    twin = make_track(session, "Same", "X", album="alt")
    twin.canon_id = canonical.id
    session.flush()

    found = find_best_track(session, canonical.title_norm, None, None)

    assert found is not None
    assert found.id == canonical.id
