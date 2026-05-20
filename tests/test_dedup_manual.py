from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from airdrome.models import DedupGroup, Track
from airdrome.normalize.dedup.manual import Deduplicator, DeduplicatorState, FilterMode, Page

from factories import make_dedup_group, make_page, make_track


# --- Page ---


def test_page_post_init_captures_canons(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id
    session.flush()

    page = Page(tracks=[a, b])

    assert page.canons == [None, a.id]
    assert page.chosen_canons == [None, a.id]


def test_page_auto_resolved_flag_when_any_canon_set(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id
    session.flush()

    assert Page(tracks=[a, b]).auto_resolved is True
    # neither has canon_id → not auto-resolved
    c = make_track(session, "C", "Y")
    d = make_track(session, "D", "Y")
    assert Page(tracks=[c, d]).auto_resolved is False


def test_page_set_canon_invalid_index_raises(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    page = make_page([a, b])

    with pytest.raises(ValueError, match="Index out of range"):
        page.set_canon(5, [0])


def test_page_set_canon_self_raises(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    page = make_page([a, b])

    with pytest.raises(ValueError, match="canon of itself"):
        page.set_canon(0, [0])


def test_page_set_canon_already_chosen_as_canon_raises(session):
    # Prevent chains: if T was already set as someone else's canon,
    # it can't now be marked as a twin of yet another canon.
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    page = make_page([a, b, c])

    page.set_canon(0, [1])  # a is canon, b is twin (b's chosen_canon = a.id)
    # now try to mark b as twin of c — but a.id is already in chosen_canons via b
    # we want to fail because making the OTHER track (a, at idx 0) a twin would chain.
    # The check: `if members[member_idx] in self.chosen_canons: raise`
    # members[0] = a.id; a.id is in chosen_canons (via b at idx 1). So setting member_idx=0 raises.
    with pytest.raises(ValueError, match="Already chosen as a canon"):
        page.set_canon(2, [0])


def test_page_set_canon_double_assign_raises(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    page = make_page([a, b, c])

    page.set_canon(0, [1])  # a canon of b
    # try to reassign b as canon — b already has a canon (a)
    with pytest.raises(ValueError, match="Already has a canon"):
        page.set_canon(1, [2])


def test_page_multi_canon_within_one_page(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    d = make_track(session, "D", "X")
    page = make_page([a, b, c, d])

    page.set_canon(0, [1])  # a canon of b
    page.set_canon(2, [3])  # c canon of d (separate sub-canon)

    assert page.chosen_canons == [None, a.id, None, c.id]


def test_page_confirm_clears_auto_resolved(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id
    session.flush()
    page = Page(tracks=[a, b])
    assert page.auto_resolved is True

    page.confirm()

    assert page.confirmed is True
    assert page.auto_resolved is False


def test_page_reset_clears_all(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id
    session.flush()
    page = Page(tracks=[a, b])
    page.confirmed = True
    page.auto_resolved = True

    page.reset()

    assert page.confirmed is False
    assert page.auto_resolved is False
    assert page.chosen_canons == [None, None]


# --- DeduplicatorState ---


def _state_with_pages(**pages_kw) -> DeduplicatorState:
    """Build a state directly from {key: (auto_resolved, confirmed)} pairs.

    Doesn't need the DB — synthetic Page objects with no real tracks.
    """
    pages = {}
    for key, (auto, conf) in pages_kw.items():
        page = Page(tracks=[])
        page.auto_resolved = auto
        page.confirmed = conf
        pages[key] = page
    return DeduplicatorState(pages=pages)


@pytest.mark.parametrize(
    "mode,expected_keys",
    [
        (FilterMode.RESOLVED_ALL, {"unres_unconf", "unres_conf"}),
        (FilterMode.RESOLVED_UNCONFIRMED, {"unres_unconf"}),
        (FilterMode.RESOLVED_CONFIRMED, {"unres_conf"}),
        (FilterMode.AUTO_RESOLVED, {"auto_unconf"}),
    ],
)
def test_state_filter_mode_returns_right_subset(mode, expected_keys):
    state = _state_with_pages(
        unres_unconf=(False, False),
        unres_conf=(False, True),
        auto_unconf=(True, False),
        auto_conf=(True, True),
    )
    state.filter_mode = mode

    keys = {k for k, _ in state.filtered_pages()}
    assert keys == expected_keys


def test_state_partial_match_filters_by_substring(session):
    a = make_track(session, "Bohemian Rhapsody", "Queen")
    b = make_track(session, "Bohemian Rhapsody", "Queen", "Opera")
    c = make_track(session, "Hotel California", "Eagles")
    d = make_track(session, "Hotel California", "Eagles", "Hell Freezes")

    state = DeduplicatorState(
        pages={"bohemian": Page(tracks=[a, b]), "hotel": Page(tracks=[c, d])},
        partial_match="bohemian",
    )

    keys = {k for k, _ in state.filtered_pages()}
    assert keys == {"bohemian"}


def test_state_switch_mode_restores_per_mode_idx():
    state = _state_with_pages(
        a=(False, False),
        b=(False, False),
        c=(False, False),
    )
    state.current_idx = 2
    state.switch_mode()  # → RESOLVED_UNCONFIRMED (same 3 pages, since none confirmed)
    state.current_idx = 0
    state.switch_mode()  # → RESOLVED_CONFIRMED (0 pages)
    state.switch_mode()  # → AUTO_RESOLVED (0 pages)
    state.switch_mode()  # → back to RESOLVED_ALL — restores idx=2

    assert state.filter_mode == FilterMode.RESOLVED_ALL
    assert state.current_idx == 2


def test_state_clamp_adjusts_on_filter_shrink():
    state = _state_with_pages(
        a=(False, False),
        b=(False, False),
        c=(False, False),
    )
    # current_idx beyond the filtered total → clamp pulls it back to total-1
    state.current_idx = 5
    state.filter_mode = FilterMode.RESOLVED_UNCONFIRMED  # 3 pages
    state.clamp()
    assert state.current_idx == 2

    # total=0 case: clamp leaves current_idx as-is
    state.filter_mode = FilterMode.RESOLVED_CONFIRMED  # 0 pages
    state.clamp()
    assert state.current_idx == 2


def test_state_go_next_prev_clamps_at_bounds():
    state = _state_with_pages(a=(False, False), b=(False, False))
    state.current_idx = 0

    assert state.go_next() is True
    assert state.current_idx == 1
    assert state.go_next() is False  # already at last
    assert state.current_idx == 1
    assert state.go_prev() is True
    assert state.current_idx == 0
    assert state.go_prev() is False
    assert state.current_idx == 0


# --- Deduplicator ---


def test_get_track_groups_excludes_singletons(session):
    make_track(session, "Solo", "A")

    d = Deduplicator(session)
    assert d.get_track_groups([Track.artist_norm, Track.title_norm]) == []


def test_get_track_groups_skips_empty_norm_fields(session):
    # Two tracks share title "A" but have no artist/album_artist/album.
    # Without the skip-empty guard, they'd collapse into a giant group
    # keyed by (artist_norm="", title_norm="a").
    make_track(session, "A")
    make_track(session, "A")

    d = Deduplicator(session)
    d.fill_state()

    assert d.state.pages == {}


def test_get_track_groups_sort_order_canon_first(session):
    early = make_track(session, "S", "A", date_added=datetime(2020, 1, 1, tzinfo=timezone.utc))
    late = make_track(session, "S", "A", date_added=datetime(2024, 1, 1, tzinfo=timezone.utc), album="X")

    d = Deduplicator(session)
    groups = d.get_track_groups([Track.artist_norm, Track.title_norm])

    assert len(groups) == 1
    [(_, tracks)] = groups
    assert tracks[0].id == early.id
    assert tracks[1].id == late.id


def test_fill_state_uses_all_column_sets(session):
    # Pair only via artist+title:
    a1 = make_track(session, "Song1", "A", album="P")
    a2 = make_track(session, "Song1", "A", album="Q")
    # Pair only via album+title:
    b1 = make_track(session, "Song2", "X", album="Same")
    b2 = make_track(session, "Song2", "Y", album="Same")

    d = Deduplicator(session)
    d.fill_state()

    # Expect at least two pages — one per pairing
    track_id_sets = [{t.id for t in p.tracks} for p in d.state.pages.values()]
    assert {a1.id, a2.id} in track_id_sets
    assert {b1.id, b2.id} in track_id_sets


def test_fill_state_merges_overlapping_groups(session):
    # T1 and T2 share artist+title; T2 and T3 share album+title.
    # The overlapping shared track (T2) should collapse them into one page.
    t1 = make_track(session, "S", "ArtA", album="AlbX")
    t2 = make_track(session, "S", "ArtA", album="AlbY")
    t3 = make_track(session, "S", "ArtB", album="AlbY")

    d = Deduplicator(session)
    d.fill_state()

    # Find the page containing all three
    pages = [p for p in d.state.pages.values() if {t.id for t in p.tracks} == {t1.id, t2.id, t3.id}]
    assert len(pages) == 1


def test_fill_state_restores_confirmed_from_db(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    make_dedup_group(session, [(t1, None), (t2, t1)], label="stored")

    d = Deduplicator(session)
    d.fill_state()

    # The matching page should come back confirmed with the right canons
    matching = [p for p in d.state.pages.values() if {t.id for t in p.tracks} == {t1.id, t2.id}]
    assert len(matching) == 1
    [page] = matching
    assert page.confirmed is True
    # chosen_canons depends on page.tracks order — assert by membership
    canon_by_id = dict(zip([t.id for t in page.tracks], page.chosen_canons))
    assert canon_by_id[t1.id] is None
    assert canon_by_id[t2.id] == t1.id


def test_apply_changes_writes_only_confirmed_pages(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    t2 = make_track(session, "Song", "Artist", "Album B")
    t3 = make_track(session, "Other", "ArtistB", "Album C")
    t4 = make_track(session, "Other", "ArtistB", "Album D")

    d = Deduplicator(session)
    d.fill_state()
    # Confirm only the first matching page
    pages_with_t1 = [(k, p) for k, p in d.state.pages.items() if any(t.id == t1.id for t in p.tracks)]
    assert pages_with_t1
    key, page = pages_with_t1[0]
    canon_idx = next(i for i, t in enumerate(page.tracks) if t.id == t1.id)
    twin_idx = next(i for i, t in enumerate(page.tracks) if t.id == t2.id)
    page.set_canon(canon_idx, [twin_idx])
    page.confirm()

    n = d.apply_changes()
    session.flush()
    session.refresh(t1)
    session.refresh(t2)
    session.refresh(t3)
    session.refresh(t4)

    assert n == 1
    assert t2.canon_id == t1.id
    assert t1.canon_id is None
    # other page wasn't confirmed → no canon writes
    assert t3.canon_id is None
    assert t4.canon_id is None


def test_apply_changes_persists_to_db(session):
    t1 = make_track(session, "Song", "Artist", "Album A")
    make_track(session, "Song", "Artist", "Album B")  # duplicate to surface a page

    d = Deduplicator(session)
    d.fill_state()
    pages_with_t1 = [p for p in d.state.pages.values() if any(t.id == t1.id for t in p.tracks)]
    page = pages_with_t1[0]
    canon_idx = next(i for i, t in enumerate(page.tracks) if t.id == t1.id)
    twin_idx = 1 - canon_idx
    page.set_canon(canon_idx, [twin_idx])
    page.confirm()

    d.apply_changes()

    # save_confirmed_groups should have inserted a row
    assert session.scalars(select(DedupGroup)).all()
