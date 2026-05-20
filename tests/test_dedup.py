from airdrome.match import find_best_track
from airdrome.models import DedupGroup, DedupGroupMember, Track
from airdrome.normalize.dedup import (
    Deduplicator,
    apply_manual_overrides,
    compute_auto_dedup_groups,
    flatten_canon_chains,
)


def _make_track(s, title: str, artist: str | None = None, album: str | None = None, **kw) -> Track:
    t = Track(title=title, artist=artist, album=album, **kw)
    s.add(t)
    s.flush()
    return t


def test_flatten_canon_chains_collapses_chain(session):
    a = _make_track(session, "A", "X")
    b = _make_track(session, "B", "X")
    c = _make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id  # chain A <- B <- C
    session.flush()

    changed = flatten_canon_chains(session)

    assert changed == 1
    session.refresh(c)
    assert c.canon_id == a.id


def test_flatten_canon_chains_is_idempotent(session):
    a = _make_track(session, "A", "X")
    b = _make_track(session, "B", "X")
    c = _make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id
    session.flush()

    flatten_canon_chains(session)
    assert flatten_canon_chains(session) == 0


def test_auto_dedup_skips_empty_title(session):
    _make_track(session, "", "X")
    _make_track(session, "", "X")

    groups = compute_auto_dedup_groups(session)

    assert groups == []


def test_manual_grouping_skips_empty_artist(session):
    # Both tracks share title "A" but have no artist/album_artist/album.
    # Without the skip-empty guard, they would collapse into a giant group
    # keyed by (artist_norm="", title_norm="a").
    _make_track(session, "A")
    _make_track(session, "A")

    dedup = Deduplicator(session)
    dedup.fill_state()

    assert dedup.state.pages == {}


def test_find_best_track_prefers_canonical_on_tie(session):
    canonical = _make_track(session, "Same", "X")
    twin = _make_track(session, "Same", "X")
    twin.canon_id = canonical.id
    session.flush()

    found = find_best_track(session, canonical.title_norm, None, None)

    assert found is not None
    assert found.id == canonical.id


def test_apply_manual_overrides_applies_stored_choice(session):
    t1 = _make_track(session, "Song", "Artist", "Album A")
    t2 = _make_track(session, "Song", "Artist", "Album B")

    group = DedupGroup(label="manual-test")
    group.members = [
        DedupGroupMember(member_hash=t1.duplicate_hash, canon_hash=None),
        DedupGroupMember(member_hash=t2.duplicate_hash, canon_hash=t1.duplicate_hash),
    ]
    session.add(group)
    session.flush()

    changed = apply_manual_overrides(session)

    assert changed == 1
    session.refresh(t2)
    assert t2.canon_id == t1.id
    session.refresh(t1)
    assert t1.canon_id is None


def test_dump_load_roundtrip(session):
    t1 = _make_track(session, "Song", "Artist", "Album A")
    _make_track(session, "Song", "Artist", "Album B")

    dedup = Deduplicator(session)
    dedup.fill_state()
    # The two tracks share artist+title, so manual grouping surfaces them.
    assert dedup.state.pages, "expected at least one duplicate page"
    key = next(iter(dedup.state.pages))
    page = dedup.state.pages[key]
    canon_idx = next(i for i, t in enumerate(page.tracks) if t.id == t1.id)
    twin_idx = 1 - canon_idx
    page.set_canon(canon_idx, [twin_idx])
    page.confirm()

    dedup.apply_changes()
    session.flush()

    # Round-trip: a fresh Deduplicator should restore the confirmed pick.
    fresh = Deduplicator(session)
    fresh.fill_state()
    fresh_page = next(iter(fresh.state.pages.values()))
    assert fresh_page.confirmed
    canon_idx_fresh = next(i for i, t in enumerate(fresh_page.tracks) if t.id == t1.id)
    assert fresh_page.chosen_canons[canon_idx_fresh] is None
    twin_idx_fresh = 1 - canon_idx_fresh
    assert fresh_page.chosen_canons[twin_idx_fresh] == t1.id
