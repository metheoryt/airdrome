import random
from pathlib import Path

import pytest

from airdrome.enums import BindMethod, DecisionKind
from airdrome.library import fingerprint
from airdrome.library.ladder import LadderContext, _link_twin, recording_of_track, resolve_file
from airdrome.library.unify import do_unify
from airdrome.models import Track, TrackFile
from airdrome.normalize.dedup.auto import auto_deduplicate
from tests.factories import add_pair, make_track


MBID = "11111111-1111-1111-1111-111111111111"
OTHER_MBID = "22222222-2222-2222-2222-222222222222"


def _fp(seed: int, n: int = 120) -> list[int]:
    """A deterministic fingerprint with values spread enough to fill the index's coarse buckets."""
    rng = random.Random(seed)
    return [rng.getrandbits(31) for _ in range(n)]


@pytest.fixture()
def add_file(session):
    """Factory persisting a TrackFile (bound to ``track`` when given) and returning it."""
    counter = iter(range(10_000))

    def _make(track: Track | None = None, **kw: object) -> TrackFile:
        tf = TrackFile(source_path=Path(f"/music/{next(counter)}.mp3"), **kw)
        if track is not None:
            tf.track = track
        session.add(tf)
        session.flush()
        return tf

    return _make


@pytest.fixture()
def album_track(session, add_file) -> Track:
    """A library track on its original album, carrying recording MBID and fingerprint 1."""
    t = make_track(session, title="Feed", artist="Rise", album="Original", album_artist="Rise")
    add_file(t, recording_mbid=MBID, mbid_source=BindMethod.MBID, fingerprint=_fp(1), fp_duration=200.0)
    return t


def _resolve(session, tf: TrackFile) -> Track | None:
    """Build a fresh context and run the ladder for one file."""
    return resolve_file(session, tf, LadderContext.build(session))


# ── recording_of_track ──────────────────────────────────────────────────────────


def test_recording_of_track_disagreeing_files_is_none(session, add_file) -> None:
    """Two files carrying different ids leave the track with no recording id."""
    t = make_track(session, title="x")
    add_file(t, recording_mbid=MBID)
    add_file(t, recording_mbid=OTHER_MBID)
    assert recording_of_track(t) is None


def test_recording_of_track_ignores_files_without_id(session, add_file) -> None:
    """Files with no id neither contribute nor veto; agreeing ones give the id."""
    t = make_track(session, title="x")
    add_file(t, recording_mbid=MBID)
    add_file(t)
    add_file(t, recording_mbid=MBID)
    assert recording_of_track(t) == MBID


def test_recording_of_track_no_ids_is_none(session, add_file) -> None:
    """A track whose files carry no id has none."""
    t = make_track(session, title="x")
    add_file(t)
    assert recording_of_track(t) is None


# ── rung (a): recording id ───────────────────────────────────────────────────────


def test_mbid_binds_to_same_album_member(session, add_file, album_track: Track) -> None:
    """Same id + same album binds to that track, even when the file's title tag differs."""
    tf = add_file(
        title="Feed (Remastered)",
        artist="Rise",
        album="original",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert _resolve(session, tf) is album_track
    assert tf.track is album_track
    assert (tf.bind_method, tf.bind_confidence) == (BindMethod.MBID, 1.0)


def test_acoustid_id_has_its_own_method(session, add_file, album_track: Track) -> None:
    """An id from the AcoustID lookup binds with method acoustid and confidence 0.9."""
    tf = add_file(title="Feed", album="Original", recording_mbid=MBID, mbid_source=BindMethod.ACOUSTID)
    assert _resolve(session, tf) is album_track
    assert (tf.bind_method, tf.bind_confidence) == (BindMethod.ACOUSTID, 0.9)


def test_mbid_other_album_creates_twin(session, add_file, album_track: Track) -> None:
    """A compilation copy becomes its own Track, linked as a twin of the album track."""
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="NFS Most Wanted",
        album_artist="Various",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    track = _resolve(session, tf)
    assert track is not None and track is not album_track
    assert (track.album, track.title) == ("NFS Most Wanted", "Feed")
    assert track.canon is album_track
    assert (track.canon_method, track.canon_confidence) == (BindMethod.MBID, 1.0)
    assert track in album_track.twins
    assert tf.bind_method == BindMethod.MBID


def test_mbid_twin_links_to_group_canon(session, add_file) -> None:
    """When the id is on a twin, a new release links to the group's canon, never to the twin."""
    canon = make_track(session, title="Feed", album="Deluxe")
    twin = make_track(session, title="Feed", album="Original", canon=canon)
    add_file(twin, recording_mbid=MBID, mbid_source=BindMethod.MBID)
    tf = add_file(title="Feed", artist="Rise", album="OST", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    track = _resolve(session, tf)
    assert track is not None and track.canon is canon


def test_mbid_same_album_finds_twin_member(session, add_file) -> None:
    """Rule 1 searches the whole group: the id is on the canon, the album match is a twin."""
    canon = make_track(session, title="Feed", album="Original")
    twin = make_track(session, title="Feed", album="OST", canon=canon)
    add_file(canon, recording_mbid=MBID, mbid_source=BindMethod.MBID)
    tf = add_file(title="Feed", album="OST", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    assert _resolve(session, tf) is twin


def test_mbid_existing_track_in_other_group_left_alone(session, add_file, album_track: Track) -> None:
    """An existing tags match that already twins elsewhere keeps its canon; the file still binds."""
    other_canon = make_track(session, title="Elsewhere")
    existing = make_track(
        session, title="Feed", artist="Rise", album="OST", album_artist="Rise", canon=other_canon
    )
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert _resolve(session, tf) is existing
    assert existing.canon is other_canon


def test_mbid_existing_canon_never_chained(session, add_file, album_track: Track) -> None:
    """An existing tags match that is itself a canon of twins is not made a twin (no chains)."""
    existing = make_track(session, title="Feed", artist="Rise", album="OST", album_artist="Rise")
    make_track(session, title="Feed live", canon=existing)
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert _resolve(session, tf) is existing
    assert existing.canon_id is None


def test_disagreeing_track_is_not_an_mbid_target(session, add_file) -> None:
    """A track whose files disagree is absent from the id index, so it cannot chain groups."""
    t = make_track(session, title="Feed", album="Original")
    add_file(t, recording_mbid=MBID)
    add_file(t, recording_mbid=OTHER_MBID)
    tf = add_file(title="Feed", album="Original 2", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    track = _resolve(session, tf)
    assert track is not None and track is not t
    assert track.canon is None
    assert tf.bind_method == BindMethod.TAGS_EXACT


def test_newly_bound_file_joins_context(session, add_file) -> None:
    """Two unbound copies of a new recording group together within one run."""
    first = add_file(title="New", album="LP", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    second = add_file(
        title="New (album version)", album="LP", recording_mbid=MBID, mbid_source=BindMethod.MBID
    )
    ctx = LadderContext.build(session)
    track = resolve_file(session, first, ctx)
    assert first.bind_method == BindMethod.TAGS_EXACT
    assert resolve_file(session, second, ctx) is track
    assert second.bind_method == BindMethod.MBID


# ── rung (b): fingerprint ────────────────────────────────────────────────────────


def test_junk_tags_bind_to_canon(session, add_file) -> None:
    """A 'Track 01' file with no album, fingerprinting as a twin's file, binds to the group canon."""
    canon = make_track(session, title="Feed", album="Deluxe")
    twin = make_track(session, title="Feed", album="Original", canon=canon)
    add_file(twin, fingerprint=_fp(1), fp_duration=200.0)
    tf = add_file(title="Track 01", fingerprint=_fp(1), fp_duration=201.0)
    assert _resolve(session, tf) is canon
    assert tf.bind_method == BindMethod.FINGERPRINT
    assert tf.bind_confidence == pytest.approx(1.0)


def test_fp_within_two_seconds_links(session, add_file, album_track: Track) -> None:
    """A fingerprint match within 2 s on another album makes a twin with method fingerprint."""
    tf = add_file(title="Feed", artist="Rise", album="OST", fingerprint=_fp(1), fp_duration=202.0)
    track = _resolve(session, tf)
    assert track is not None and track.canon is album_track
    assert track.canon_method == BindMethod.FINGERPRINT
    assert tf.bind_method == BindMethod.FINGERPRINT


def test_fp_duration_gap_not_linked(session, add_file, album_track: Track) -> None:
    """The same fingerprint 3 s apart is not linked: the file falls through to tags."""
    tf = add_file(title="Feed", artist="Rise", album="OST", fingerprint=_fp(1), fp_duration=203.0)
    track = _resolve(session, tf)
    assert track is not None and track is not album_track
    assert track.canon is None
    assert (tf.bind_method, tf.bind_confidence) == (BindMethod.TAGS_EXACT, 1.0)


def test_fp_unrelated_audio_not_linked(session, add_file, album_track: Track) -> None:
    """A different fingerprint does not match."""
    tf = add_file(title="Other", album="OST", fingerprint=_fp(2), fp_duration=200.0)
    track = _resolve(session, tf)
    assert track is not None and track.canon is None
    assert tf.bind_method == BindMethod.TAGS_EXACT


# ── rung (c): tags ───────────────────────────────────────────────────────────────


def test_no_evidence_tags_exact(session, add_file, album_track: Track) -> None:
    """No id or fingerprint: an exact tags match binds to the existing track."""
    tf = add_file(title="Feed", artist="Rise", album="Original", album_artist="Rise")
    assert _resolve(session, tf) is album_track
    assert (tf.bind_method, tf.bind_confidence) == (BindMethod.TAGS_EXACT, 1.0)


def test_no_evidence_new_track_from_tags(session, add_file) -> None:
    """No match anywhere: a new Track is made from the tags, method tags_exact."""
    tf = add_file(title="Brand New", artist="Someone", duration=181.4)
    track = _resolve(session, tf)
    assert track is not None and (track.title, track.artist, track.duration) == ("Brand New", "Someone", 181)
    assert tf.bind_method == BindMethod.TAGS_EXACT


@pytest.mark.parametrize("title", [None, "", "   "])
def test_blank_title_no_evidence_stays_unbound(session, add_file, title: str | None) -> None:
    """With nothing to go on and no title, no Track is created from nothing."""
    tf = add_file(title=title)
    assert _resolve(session, tf) is None
    assert tf.track_id is None and tf.bind_method is None


# ── do_unify stage 3 ─────────────────────────────────────────────────────────────


def test_do_unify_fingerprints_unbound_files_and_binds(
    session, add_file, album_track: Track, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stage 3 fingerprints existing unbound files lacking one, then resolves even a title-less file."""
    computed: list[Path] = []

    def fake_compute(path: Path) -> fingerprint.Fingerprint:
        computed.append(path)
        return fingerprint.Fingerprint(raw=_fp(1), duration=200.5)

    monkeypatch.setattr(fingerprint, "compute", fake_compute)
    junk = add_file()  # no tags at all
    junk.source_path = tmp_path / "junk.mp3"
    junk.source_path.touch()
    missing = add_file(title="Gone")  # path does not exist: never handed to fpcalc
    blank = add_file(title=None, fingerprint=_fp(3), fp_duration=10.0)  # has fp: not recomputed

    do_unify(session)

    assert computed == [junk.source_path]
    assert junk.track is album_track and junk.bind_method == BindMethod.FINGERPRINT
    assert blank.track_id is None  # R4: no match, no title → stays unbound
    assert missing.bind_method == BindMethod.TAGS_EXACT


def test_do_unify_retries_files_a_later_binding_enables(
    session, add_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank-title files resolved before their tagged sibling still bind in the same run."""
    monkeypatch.setattr(fingerprint, "compute", lambda _p: pytest.fail("all files already fingerprinted"))
    by_id = add_file(recording_mbid=MBID, mbid_source=BindMethod.MBID, fingerprint=_fp(5), fp_duration=50.0)
    by_fp = add_file(fingerprint=_fp(1), fp_duration=200.0)
    tagged = add_file(
        title="Feed",
        artist="Rise",
        album="Original",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
        fingerprint=_fp(1),
        fp_duration=200.0,
    )

    do_unify(session)

    assert tagged.track is not None
    assert by_id.track is tagged.track and by_id.bind_method == BindMethod.MBID
    assert by_fp.track is tagged.track and by_fp.bind_method == BindMethod.FINGERPRINT


def test_same_album_member_with_other_recording_is_skipped(session, add_file) -> None:
    """Rule 1 never binds a file onto a member whose files agree on a different recording id."""
    canon = make_track(session, title="Feed", album="Original")
    other = make_track(session, title="Feed (live)", album="OST", canon=canon)
    add_file(canon, recording_mbid=MBID, mbid_source=BindMethod.MBID)
    add_file(other, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID)
    tf = add_file(title="Track 01", album="OST", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    assert _resolve(session, tf) is canon


def test_twin_id_never_binds_to_canon_of_other_recording(session, add_file) -> None:
    """In a flag-set group whose canon is another recording, junk tags bind to the matching twin."""
    canon = make_track(session, title="Feed", album="Original")
    twin = make_track(session, title="Feed", album="Live", canon=canon)
    add_file(canon, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID)
    add_file(twin, recording_mbid=MBID, mbid_source=BindMethod.MBID)
    ctx = LadderContext.build(session)
    tf = add_file(title="Track 01", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    assert resolve_file(session, tf, ctx) is twin
    assert recording_of_track(canon) == OTHER_MBID
    assert ctx.by_mbid[OTHER_MBID] == [canon]


def test_twin_id_new_release_not_linked_to_foreign_canon(session, add_file) -> None:
    """Rule 2 under a foreign canon makes the new release but does not link it across recordings."""
    canon = make_track(session, title="Feed", album="Original")
    twin = make_track(session, title="Feed", album="Live", canon=canon)
    add_file(canon, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID)
    add_file(twin, recording_mbid=MBID, mbid_source=BindMethod.MBID)
    tf = add_file(title="Feed", artist="Rise", album="OST", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    track = _resolve(session, tf)
    assert track is not None and track not in (canon, twin)
    assert track.canon is None


def test_tags_landing_in_foreign_group_is_not_rekeyed(session, add_file, album_track: Track) -> None:
    """A rule-2 upsert onto a track of another group does not pull that group into later id matches."""
    other_canon = make_track(session, title="Elsewhere")
    existing = make_track(
        session, title="Feed", artist="Rise", album="OST", album_artist="Rise", canon=other_canon
    )
    ctx = LadderContext.build(session)
    first = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert resolve_file(session, first, ctx) is existing
    assert ctx.by_mbid[MBID] == [album_track]
    # Junk title on the same album: without the fix, rule 1 would find `existing` in the pulled-in group.
    second = add_file(title="Track 01", album="OST", recording_mbid=MBID, mbid_source=BindMethod.MBID)
    assert resolve_file(session, second, ctx) is album_track


def test_tags_hit_on_other_recording_falls_to_rule_3(session, add_file, album_track: Track) -> None:
    """A rule-2 upsert onto a track agreeing on another id is refused; the file binds to the canon."""
    existing = make_track(session, title="Feed", artist="Rise", album="OST", album_artist="Rise")
    add_file(existing, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID)
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert _resolve(session, tf) is album_track
    assert existing.canon is None
    assert recording_of_track(existing) == OTHER_MBID


def test_fp_match_of_other_recording_skipped(session, add_file, album_track: Track) -> None:
    """A file carrying id X never joins, by fingerprint, a track that agrees on id Y."""
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        recording_mbid=OTHER_MBID,
        mbid_source=BindMethod.MBID,
        fingerprint=_fp(1),
        fp_duration=200.0,
    )
    track = _resolve(session, tf)
    assert track is not None and track is not album_track
    assert track.canon is None
    assert tf.bind_method == BindMethod.TAGS_EXACT


def test_fp_match_on_twin_uses_twins_recording(session, add_file) -> None:
    """An id-less file matching a twin's fingerprint never binds to a canon of another recording."""
    canon = make_track(session, title="Feed", album="Original")
    twin = make_track(session, title="Feed", album="Live", canon=canon)
    add_file(
        canon, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID, fingerprint=_fp(9), fp_duration=200.0
    )
    add_file(twin, recording_mbid=MBID, mbid_source=BindMethod.MBID, fingerprint=_fp(1), fp_duration=200.0)
    tf = add_file(title="Track 01", fingerprint=_fp(1), fp_duration=200.0)
    assert _resolve(session, tf) is twin
    assert tf.bind_method == BindMethod.FINGERPRINT


def test_unrekeyed_track_drops_stale_key(session, add_file) -> None:
    """With rekey=False a track whose agreed id changed leaves its old key and gains no new one."""
    t = make_track(session, title="x")
    add_file(t, recording_mbid=MBID)
    ctx = LadderContext.build(session)
    tf = add_file(t, recording_mbid=OTHER_MBID)
    ctx.add(tf, t, MBID, rekey=False)
    assert ctx.by_mbid[MBID] == []
    assert OTHER_MBID not in ctx.by_mbid

    t2 = make_track(session, title="y")
    tf2 = add_file(t2, recording_mbid=OTHER_MBID)
    ctx.add(tf2, t2, None, rekey=False)
    assert OTHER_MBID not in ctx.by_mbid


def test_link_twin_refuses_foreign_id_inside_group(session, add_file) -> None:
    """The id check runs before the already-in-group shortcut, so a foreign member is refused."""
    canon = make_track(session, title="Feed", album="Original")
    member = make_track(session, title="Feed", album="OST", canon=canon)
    add_file(canon, recording_mbid=MBID)
    add_file(member, recording_mbid=OTHER_MBID)
    assert _link_twin(member, canon, MBID, BindMethod.MBID, 1.0) is False


# ── conflicted tracks (M-2) ──────────────────────────────────────────────────────


def _conflicted(
    session, add_file, *, title: str, artist: str, album: str, album_artist: str | None = None
) -> Track:
    """A track whose two files disagree on the recording id; the first carries fingerprint 1."""
    t = make_track(session, title=title, artist=artist, album=album, album_artist=album_artist)
    add_file(t, recording_mbid=MBID, fingerprint=_fp(1), fp_duration=200.0)
    add_file(t, recording_mbid=OTHER_MBID)
    return t


def test_fp_match_on_conflicted_track_refused(session, add_file) -> None:
    """An id-less file whose audio matches a conflicted track is not bound there: it falls to tags."""
    t = _conflicted(session, add_file, title="Feed", artist="Rise", album="Original")
    tf = add_file(title="Feed", artist="Rise", album="Bootleg", fingerprint=_fp(1), fp_duration=200.0)
    track = _resolve(session, tf)
    assert track is not None and track is not t
    assert tf.bind_method == BindMethod.TAGS_EXACT


def test_tags_hit_on_conflicted_track_falls_to_rule_3(session, add_file, album_track: Track) -> None:
    """Rule 2 refuses a tags hit on a conflicted track, as dedup leaves it out of every group."""
    t = _conflicted(session, add_file, title="Feed", artist="Rise", album="OST", album_artist="Rise")
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    assert _resolve(session, tf) is album_track
    assert t.canon is None


# ── backfill never moves a hash (I-1) ────────────────────────────────────────────


def test_backfill_fills_hashed_fields_of_decided_track(session, add_file) -> None:
    """Decisions name tracks by natural key, so a file filling year/duration moves no decision.

    Regression guard: it may already have passed before the group tables went away.
    """
    member = make_track(session, title="Feed", artist="Rise", album="Original", album_artist="Rise")
    other = make_track(session, title="Feed", artist="Rise", album="OST", album_artist="Rise")
    add_pair(session, member, other, DecisionKind.SAME)
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="Original",
        album_artist="Rise",
        date="1999",
        duration=200.0,
        bitrate=128_000,
    )

    assert _resolve(session, tf) is member
    assert (member.year, member.duration) == (1999, 200)

    auto_deduplicate(session)
    assert sorted(t.canon_id is None for t in (member, other)) == [False, True]


def test_tags_hit_without_decision_is_backfilled(session, add_file) -> None:
    """A track no decision names takes year and duration from its new file (fresh-DB land)."""
    t = make_track(session, title="Feed", artist="Rise", album="Original", album_artist="Rise")
    tf = add_file(
        title="Feed", artist="Rise", album="Original", album_artist="Rise", date="1999", duration=200.0
    )
    assert _resolve(session, tf) is t
    assert (t.year, t.duration) == (1999, 200)


def test_refused_tags_hit_edits_nothing(session, add_file, album_track: Track) -> None:
    """A rule-2 hit on another recording's track is refused before any backfill touches it."""
    existing = make_track(session, title="Feed", artist="Rise", album="OST", album_artist="Rise")
    add_file(existing, recording_mbid=OTHER_MBID, mbid_source=BindMethod.MBID)
    tf = add_file(
        title="Feed",
        artist="Rise",
        album="OST",
        album_artist="Rise",
        date="1999",
        duration=200.0,
        recording_mbid=MBID,
        mbid_source=BindMethod.MBID,
    )
    ctx = LadderContext.build(session)
    assert resolve_file(session, tf, ctx) is album_track
    assert (existing.year, existing.duration) == (None, None)
    assert ctx.stats["updated"] == 0
