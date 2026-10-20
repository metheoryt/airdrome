"""Recording identity (shared recording id / fingerprint) as a `dedup` grouping input."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from airdrome import console as console_mod
from airdrome.enums import BindMethod, DecisionKind
from airdrome.library.fingerprint import FP_MATCH, similarity
from airdrome.models import Track, TrackFile
from airdrome.normalize.dedup import RECOMMENDED_SETS
from airdrome.normalize.dedup.auto import auto_deduplicate
from airdrome.normalize.dedup.grouping import CanonStrategy
from airdrome.normalize.dedup.recording import compute_recording_groups, scan_recordings

from factories import add_canon, add_pair, make_track, old_hash


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


def test_not_duplicates_veto_transitive(session, track, add_file) -> None:
    """A and B decided different; C shares their id: A and B still end apart.

    A dead decision (naming a track no longer in the library) beside it changes nothing.
    """
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2020)  # earliest: the recording group's canon, linked to both
    gone = track(title="Gone")
    for t in (a, b, c):
        add_file(t, recording_mbid=X)
    add_pair(session, a, b, DecisionKind.DIFFERENT)
    add_pair(session, a, gone, DecisionKind.SAME)
    session.delete(gone)
    session.flush()

    result = _dedup(session)

    assert not _together(result.groups, a, b)
    assert _root(a) != _root(b)
    assert result.dead == 1


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
    """Recording grouping links tracks without touching the fields the old group hash is built from."""
    tracks = [track(added=2020 + i) for i in range(3)]
    for t in tracks:
        add_file(t, recording_mbid=X)
    before = {t.id: old_hash(t) for t in tracks}

    auto_deduplicate(session, flag_sets=RECOMMENDED_SETS, strategy=CanonStrategy.YEAR)

    assert {t.id: old_hash(t) for t in tracks} == before
    assert sum(t.canon_id is not None for t in tracks) == 2


# ── newcomers to manually decided groups ────────────────────────────────────────


def test_newcomer_joins_under_chosen_canon(session, track, add_file) -> None:
    """A track recording identity adds to a manual group joins under that group's canon choice.

    N sorts first, so canon order alone would make it canon; the canon choice keeps A. A's core is
    A alone (no automatic evidence reaches it), so B and N are both folded in: MANUAL links.
    """
    a = track(added=2021)
    b = track(added=2022)
    n = track(added=2019)
    add_file(b, recording_mbid=X)
    add_file(n, recording_mbid=X)
    add_pair(session, a, b, DecisionKind.SAME)
    add_canon(session, a)

    _dedup(session)

    assert (a.canon_id, b.canon_id, n.canon_id) == (None, a.id, a.id)
    assert (n.canon_method, n.canon_confidence, n.canon_folded) == (BindMethod.MANUAL, 1.0, True)
    assert b.canon_method == BindMethod.MANUAL


def test_newcomer_without_canon_choice_leads_by_canon_order(session, track, add_file) -> None:
    """Without a canon choice, the newcomer N that sorts first becomes the canon of the joined group."""
    a = track(added=2021)
    b = track(added=2022)
    n = track(added=2019)
    add_file(b, recording_mbid=X)
    add_file(n, recording_mbid=X)
    add_pair(session, a, b, DecisionKind.SAME)

    _dedup(session)

    assert (n.canon_id, a.canon_id, b.canon_id) == (None, n.id, n.id)
    assert (b.canon_method, b.canon_folded) == (BindMethod.MBID, False)  # in N's core: recording evidence
    assert (a.canon_method, a.canon_folded) == (BindMethod.MANUAL, True)


def test_newcomer_joins_one_side_of_not_duplicates(session, track, add_file) -> None:
    """With A, B decided different, a newcomer sharing their id joins one of them, not both."""
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2019)
    for t in (a, b, c):
        add_file(t, recording_mbid=X)
    add_pair(session, a, b, DecisionKind.DIFFERENT)

    _dedup(session)

    assert _root(a) != _root(b)
    assert _root(c) in {_root(a), _root(b)}


def test_manual_same_never_bypasses_the_veto(session, track, add_file) -> None:
    """same(A,B) and different(A,C): C, sharing B's id, is kept from the whole manual group."""
    a = track(added=2021)
    b = track(added=2022)
    c = track(added=2019)
    add_file(b, recording_mbid=X)
    add_file(c, recording_mbid=X)
    add_pair(session, a, b, DecisionKind.SAME)
    add_pair(session, a, c, DecisionKind.DIFFERENT)

    _dedup(session)

    assert _root(c) != _root(a)
    assert b.canon_id == a.id
