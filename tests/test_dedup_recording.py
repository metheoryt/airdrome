"""Recording identity (shared recording id / fingerprint) as a `dedup` grouping input."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from airdrome import console as console_mod
from airdrome.enums import BindMethod
from airdrome.library.fingerprint import FP_MATCH, similarity
from airdrome.models import Track, TrackFile
from airdrome.normalize.dedup import RECOMMENDED_SETS
from airdrome.normalize.dedup.auto import auto_deduplicate
from airdrome.normalize.dedup.grouping import CanonStrategy
from airdrome.normalize.dedup.persistence import stored_cannot_links
from airdrome.normalize.dedup.recording import compute_recording_groups, scan_recordings

from factories import make_dedup_group, make_track


X = "11111111-1111-1111-1111-111111111111"
Y = "22222222-2222-2222-2222-222222222222"


def _fp(seed: int, n: int = 120) -> list[int]:
    """A deterministic fingerprint with values spread enough to fill the index's coarse buckets."""
    rng = random.Random(seed)
    return [rng.getrandbits(31) for _ in range(n)]


def _at(year: int) -> datetime:
    """A UTC date_added in `year`, so tests control the ADDED canon order."""
    return datetime(year, 1, 1, tzinfo=UTC)


@pytest.fixture()
def track(session):
    """Factory for tracks the flag sets never group: each gets its own artist, year and duration.

    Recording identity ignores tags, so isolating the flag sets this way means any group a test
    observes came from the recording pass.
    """
    counter = iter(range(1, 10_000))

    def _make(title: str = "Feed", added: int = 2020, **kw) -> Track:
        n = next(counter)
        kw.setdefault("artist", f"Artist {n}")
        kw.setdefault("album", f"Album {n}")
        return make_track(session, title, year=1900 + n, duration=60 * n, date_added=_at(added), **kw)

    return _make


@pytest.fixture()
def add_file(session):
    """Factory persisting a TrackFile bound to `track` and returning it."""
    counter = iter(range(10_000))

    def _make(track: Track, **kw) -> TrackFile:
        kw.setdefault("bitrate", 320_000)  # `dedup` re-picks main files, which ranks on bitrate
        tf = TrackFile(source_path=Path(f"/music/rec/{next(counter)}.mp3"), **kw)
        tf.track = track
        session.add(tf)
        session.flush()
        return tf

    return _make


def _dedup(session):
    """Run the batch pass with the CLI's default flag sets."""
    return auto_deduplicate(session, flag_sets=RECOMMENDED_SETS)


def _root(t: Track) -> int:
    """The id of `t`'s group root (canon links are terminal)."""
    return t.canon_id or t.id


def _together(groups: list[list[Track]], a: Track, b: Track) -> bool:
    """Whether some group holds both `a` and `b`."""
    return any(a in g and b in g for g in groups)


# ── recording id ────────────────────────────────────────────────────────────────


def test_same_mbid_two_albums_one_group(session, track, add_file) -> None:
    """One recording on its album and on a lone compilation track: one group, the album is canon."""
    album = track(album="Original", album_artist="Rise", added=2022)
    # The compilation copy was added earlier, so only the compilation rule can put the album first.
    comp = track(album="NFS Most Wanted", album_artist="Various Artists", compilation=True, added=2020)
    add_file(album, recording_mbid=X)
    add_file(comp, recording_mbid=X)

    result = _dedup(session)

    assert comp.canon_id == album.id
    assert album.canon_id is None
    assert (comp.canon_method, comp.canon_confidence) == (BindMethod.MBID, 1.0)
    assert _together(result.groups, album, comp)


def test_acoustid_id_link_is_labelled_acoustid(session, track, add_file) -> None:
    """I-2: an id one side got from an AcoustID lookup links as `acoustid` (0.9), still an id group."""
    album = track(album="Original", added=2020)
    other = track(album="Bootleg", added=2022)
    add_file(album, recording_mbid=X, mbid_source=BindMethod.MBID)
    add_file(other, recording_mbid=X, mbid_source=BindMethod.ACOUSTID)

    ((label, _),) = scan_recordings(session).auto
    _dedup(session)

    assert label.startswith("rec[mbid]")
    assert other.canon_id == album.id
    assert (other.canon_method, other.canon_confidence) == (BindMethod.ACOUSTID, 0.9)


def test_tagged_first_track_keeps_its_strongest_id_link(session, track, add_file) -> None:
    """The first track of an id pairs with every later one; a later lookup pair never downgrades it."""
    first = track(album="A", added=2020)
    tagged = track(album="B", added=2021)
    looked_up = track(album="C", added=2022)
    add_file(first, recording_mbid=X, mbid_source=BindMethod.MBID)
    add_file(tagged, recording_mbid=X, mbid_source=BindMethod.MBID)
    add_file(looked_up, recording_mbid=X, mbid_source=BindMethod.ACOUSTID)

    scan = scan_recordings(session)

    assert scan.evidence[first.id] == (BindMethod.MBID, 1.0)
    assert scan.evidence[tagged.id] == (BindMethod.MBID, 1.0)
    assert scan.evidence[looked_up.id] == (BindMethod.ACOUSTID, 0.9)


def test_feed_the_addiction_stays_apart(session, track, add_file) -> None:
    """The album and NFS cuts: different ids and fingerprints below threshold never group."""
    album = track(album="Original")
    nfs = track(album="NFS Most Wanted", album_artist="Various Artists")
    add_file(album, recording_mbid=X, fingerprint=_fp(1), fp_duration=200.0)
    add_file(nfs, recording_mbid=Y, fingerprint=_fp(2), fp_duration=200.0)
    assert similarity(_fp(1), _fp(2)) < FP_MATCH

    _dedup(session)

    assert album.canon_id is None and nfs.canon_id is None


def test_compilation_canon_when_two_or_more(session, track, add_file) -> None:
    """Two tracks from one compilation make the compilation copy canon over the earlier album."""
    album = track(album="Original", album_artist="Rise", added=2018)
    nfs = track(album="NFS Most Wanted", album_artist="Various Artists", added=2022)
    track(title="Other", album="NFS Most Wanted", album_artist="Various Artists", added=2022)
    add_file(album, recording_mbid=X)
    add_file(nfs, recording_mbid=X)

    _dedup(session)

    assert album.canon_id == nfs.id
    assert nfs.canon_id is None


# ── fingerprint ─────────────────────────────────────────────────────────────────


def test_fingerprint_within_two_seconds_links_with_score(session, track, add_file) -> None:
    """A fingerprint match ≤ 2 s apart links automatically; confidence is the similarity score."""
    a = track(added=2020)
    b = track(added=2021)
    add_file(a, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, fingerprint=_fp(1), fp_duration=202.0)

    _dedup(session)

    assert b.canon_id == a.id
    assert b.canon_method == BindMethod.FINGERPRINT
    assert b.canon_confidence == pytest.approx(1.0)


def test_fingerprint_gap_goes_to_review(session, track, add_file) -> None:
    """A fingerprint match more than 2 s apart is a review candidate, never an automatic link."""
    a = track(added=2020)
    b = track(added=2021)
    add_file(a, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, fingerprint=_fp(1), fp_duration=203.0)

    auto, review = compute_recording_groups(session)
    _dedup(session)

    assert auto == []
    assert review == [[a, b]]
    assert b.canon_id is None


def test_fingerprint_never_joins_two_recording_ids(session, track, add_file) -> None:
    """Matching audio does not link tracks whose files agree on different recording ids."""
    a = track()
    b = track()
    add_file(a, recording_mbid=X, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, recording_mbid=Y, fingerprint=_fp(1), fp_duration=200.0)

    auto, review = compute_recording_groups(session)

    assert auto == [] and review == []


def test_fingerprint_bridge_cannot_chain_two_ids(session, track, add_file) -> None:
    """An id-less track matching both an X and a Y track joins at most one of them."""
    a = track(added=2020)
    b = track(added=2021)
    bridge = track(added=2022)
    add_file(a, recording_mbid=X, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, recording_mbid=Y, fingerprint=_fp(1), fp_duration=200.0)
    add_file(bridge, fingerprint=_fp(1), fp_duration=200.0)

    auto, _ = compute_recording_groups(session)

    assert not _together(auto, a, b)


# ── manual veto ─────────────────────────────────────────────────────────────────


def test_stored_cannot_links_pairs_members_with_different_canons(session, track) -> None:
    """Members resolving to different canons are cannot-link pairs; a twin and its canon are not."""
    a, b, c = track(), track(), track()
    make_dedup_group(session, [(a, None), (b, a), (c, None)])

    assert stored_cannot_links(session) == {frozenset({a.id, c.id}), frozenset({b.id, c.id})}


def test_not_duplicates_veto_transitive(session, track, add_file) -> None:
    """A and B stored as "not duplicates"; C shares their id: A and B still end apart.

    The stored group also names a member that is no longer in the library, so the manual override
    does not apply and only the cannot-link veto keeps A and B apart.
    """
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2020)  # earliest: the recording group's canon, linked to both
    gone = track(title="Gone")
    for t in (a, b, c):
        add_file(t, recording_mbid=X)
    make_dedup_group(session, [(a, None), (b, None), (gone, None)])
    session.delete(gone)
    session.flush()

    result = _dedup(session)

    assert not _together(result.groups, a, b)
    assert _root(a) != _root(b)


# ── conflicts ───────────────────────────────────────────────────────────────────


def test_conflicted_track_does_not_chain(session, track, add_file, capsys) -> None:
    """A track whose files disagree on the id joins no recording group and is reported."""
    a = track(added=2020)
    b = track(added=2021)
    conflicted = track(title="Torn", added=2019)
    add_file(a, recording_mbid=X, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, recording_mbid=Y, fingerprint=_fp(2), fp_duration=200.0)
    add_file(conflicted, recording_mbid=X, fingerprint=_fp(1), fp_duration=200.0)
    add_file(conflicted, recording_mbid=Y, fingerprint=_fp(2), fp_duration=200.0)

    console_mod.set_verbosity(1)
    try:
        result = _dedup(session)
    finally:
        console_mod.set_verbosity(0)

    assert conflicted.canon_id is None and a.canon_id is None and b.canon_id is None
    assert not any(conflicted in g for g in result.groups)
    assert result.conflicted == 1
    assert "Torn" in capsys.readouterr().out


# ── stability ───────────────────────────────────────────────────────────────────


def test_recording_groups_survive_rerun(session, track, add_file) -> None:
    """A second `dedup` reproduces every recording link exactly."""
    a, b, c = track(added=2020), track(added=2021), track(added=2022)
    add_file(a, recording_mbid=X)
    add_file(b, recording_mbid=X)
    add_file(c, fingerprint=_fp(3), fp_duration=100.0)
    add_file(a, fingerprint=_fp(3), fp_duration=101.0)

    def snapshot() -> dict[int, tuple]:
        """Every track's canon link with its method and confidence."""
        tracks = session.query(Track).all()
        return {t.id: (t.canon_id, t.canon_method, t.canon_confidence) for t in tracks}

    _dedup(session)
    first = snapshot()
    _dedup(session)

    assert snapshot() == first
    assert first[b.id][:2] == (a.id, BindMethod.MBID)
    assert first[c.id][:2] == (a.id, BindMethod.FINGERPRINT)


def test_mirror_hashes_unchanged(session, track, add_file) -> None:
    """Recording grouping links tracks without touching the fields `duplicate_hash` is built from."""
    tracks = [track(added=2020 + i) for i in range(3)]
    for t in tracks:
        add_file(t, recording_mbid=X)
    before = {t.id: t.duplicate_hash for t in tracks}

    auto_deduplicate(session, flag_sets=RECOMMENDED_SETS, strategy=CanonStrategy.YEAR)

    assert {t.id: t.duplicate_hash for t in tracks} == before
    assert sum(t.canon_id is not None for t in tracks) == 2


def test_review_shows_fingerprint_candidates_as_unconfirmed_pages(session, track, add_file) -> None:
    """`dedup --review` offers a larger-gap fingerprint match as an unconfirmed page."""
    from airdrome.normalize.dedup.manual import Deduplicator

    a = track(added=2020)
    b = track(added=2021)
    add_file(a, fingerprint=_fp(1), fp_duration=200.0)
    add_file(b, fingerprint=_fp(1), fp_duration=210.0)

    dd = Deduplicator(session)
    dd.fill_state()

    [page] = [p for p in dd.state.pages.values() if a in p.tracks]
    assert page.tracks == [a, b]
    assert not page.confirmed and not page.auto_resolved


# ── newcomers to hand-made groups ───────────────────────────────────────────────


def test_newcomer_joins_under_stored_canon(session, track, add_file) -> None:
    """A track recording identity adds to a hand-made group joins under that group's stored canon.

    N sorts first, so it would be the automatic canon; the stored choice (A canon, B→A) wins and N
    becomes A's twin instead of being stranded on its own when the override re-points A and B.
    """
    a = track(added=2021)
    b = track(added=2022)
    n = track(added=2019)
    add_file(b, recording_mbid=X)
    add_file(n, recording_mbid=X)
    make_dedup_group(session, [(a, None), (b, a)])

    _dedup(session)

    assert (a.canon_id, b.canon_id, n.canon_id) == (None, a.id, a.id)
    assert (n.canon_method, n.canon_confidence) == (BindMethod.MBID, 1.0)
    assert b.canon_method == BindMethod.MANUAL


def test_newcomer_joins_one_side_of_not_duplicates(session, track, add_file) -> None:
    """With A, B stored as "not duplicates", a newcomer sharing their id joins one of them, not neither."""
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2019)
    for t in (a, b, c):
        add_file(t, recording_mbid=X)
    make_dedup_group(session, [(a, None), (b, None)])

    _dedup(session)

    assert a.canon_id is None and b.canon_id is None
    assert c.canon_id in {a.id, b.id}


def test_stored_canon_never_bypasses_the_veto(session, track, add_file) -> None:
    """A newcomer is not anchored onto a stored canon that a "not duplicates" choice keeps it from.

    Stored: {A canon, B→A}, and a partial "not duplicates" {A, C, gone}. B and C share an id.
    """
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2019)
    gone = track(title="Gone", added=2018)
    add_file(b, recording_mbid=X)
    add_file(c, recording_mbid=X)
    make_dedup_group(session, [(a, None), (b, a)])
    make_dedup_group(session, [(a, None), (c, None), (gone, None)])
    session.delete(gone)
    session.flush()

    _dedup(session)

    assert _root(c) != _root(a)
    assert b.canon_id == a.id


def test_flag_set_newcomer_keeps_canon_order(session) -> None:
    """A flag-set group is not re-canoned onto a member that is a stored canon elsewhere.

    R is a single-track compilation copy (lowest canon rank) and the root of an unrelated
    hand-made group; N, the album copy, shares R's flags and stays canon of that pair.
    """
    n = make_track(session, "Song", "Band", "Album", album_artist="Band", duration=200, date_added=_at(2022))
    r = make_track(
        session, "Song", "Band", "Hits", album_artist="Various Artists", duration=200, date_added=_at(2019)
    )
    q = make_track(session, "Elsewhere", "Other", "X", duration=50)
    make_dedup_group(session, [(r, None), (q, r)])

    result = _dedup(session)

    assert any(g[0] is n and r in g for g in result.groups)
    assert n.canon_id is None
    assert r.canon_id is None  # the override keeps R the root of its own hand-made group


def test_flag_reached_stored_canon_does_not_recanon_a_recording_group(session, add_file) -> None:
    """A stored canon reached only by the flag sets does not re-canon a group recording identity touched.

    B is the album copy; R, a Various Artists copy with B's flags, is the root of an unrelated
    hand-made group {R, Q}; N joins B only through a shared recording id.
    """
    b = make_track(session, "Song", "Band", "Album", album_artist="Band", duration=200, date_added=_at(2022))
    r = make_track(
        session, "Song", "Band", "Hits", album_artist="Various Artists", duration=200, date_added=_at(2019)
    )
    n = make_track(
        session, "Song (Remaster)", "Band X", "Other", duration=900, year=1999, date_added=_at(2023)
    )
    q = make_track(session, "Elsewhere", "Other", "X", duration=50)
    add_file(b, recording_mbid=X)
    add_file(n, recording_mbid=X)
    make_dedup_group(session, [(r, None), (q, r)])

    _dedup(session)

    assert b.canon_id is None
    assert n.canon_id == b.id
