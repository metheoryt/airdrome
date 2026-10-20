"""Tests for group-wide main-file selection and recompute on canon changes."""

from datetime import UTC, datetime
from pathlib import Path

from airdrome.enums import DecisionKind
from airdrome.models import TrackFile, TrackGroup
from airdrome.normalize.dedup.auto import auto_deduplicate
from airdrome.normalize.dedup.persistence import recompute_main_files

from factories import add_pair, make_track


def _add_file(s, track, name: str, bitrate: int, ext: str = "mp3", is_main: bool = False) -> TrackFile:
    tf = TrackFile(
        source_path=Path(f"/src/{name}.{ext}"), track_id=track.id, bitrate=bitrate, is_main=is_main
    )
    s.add(tf)
    s.flush()
    return tf


# --- select_main_file (shared selection) ---


def test_select_main_prefers_highest_bitrate(session):
    a = make_track(session, "S", "A")
    lo = _add_file(session, a, "lo", 128000)
    hi = _add_file(session, a, "hi", 320000)

    assert TrackGroup.select_main_file([lo, hi]) is hi


def test_select_main_breaks_bitrate_tie_by_container(session):
    a = make_track(session, "S", "A")
    mp3 = _add_file(session, a, "x", 320000, ext="mp3")
    flac = _add_file(session, a, "x", 320000, ext="flac")

    assert TrackGroup.select_main_file([mp3, flac]) is flac


def test_select_main_unknown_extension_does_not_raise(session):
    a = make_track(session, "S", "A")
    wav = _add_file(session, a, "x", 320000, ext="wav")
    mp3 = _add_file(session, a, "y", 320000, ext="mp3")

    # mp3 (priority 1) beats the unknown wav (priority 0) at equal bitrate.
    assert TrackGroup.select_main_file([wav, mp3]) is mp3


# --- TrackGroup.recompute_main ---


def test_recompute_main_picks_best_across_canon_and_twin(session):
    canon = make_track(session, "S", "A")
    twin = make_track(session, "S", "A", album="other")
    twin.canon_id = canon.id
    canon_file = _add_file(session, canon, "canon", 128000, is_main=True)
    twin_file = _add_file(session, twin, "twin", 320000)  # best copy lives on the twin
    session.flush()

    chosen = TrackGroup.of(canon).recompute_main()

    assert chosen is twin_file
    assert twin_file.is_main is True
    assert canon_file.is_main is False


def test_recompute_main_returns_none_when_no_files(session):
    canon = make_track(session, "S", "A")

    assert TrackGroup.of(canon).recompute_main() is None


# --- recompute_main_files (whole-library pass) ---


def test_recompute_collapses_two_mains_after_merge(session):
    # Two tracks each organized independently, each owning an is_main file, then
    # merged into one dedup group — the group must end up with exactly one main.
    canon = make_track(session, "S", "A")
    twin = make_track(session, "S", "A", album="other")
    canon_file = _add_file(session, canon, "canon", 128000, is_main=True)
    twin_file = _add_file(session, twin, "twin", 320000, is_main=True)
    twin.canon_id = canon.id
    session.flush()

    changed = recompute_main_files(session)

    assert changed == 1
    mains = [f for f in (canon_file, twin_file) if f.is_main]
    assert mains == [twin_file]  # best copy wins, only one survives


def test_recompute_is_idempotent(session):
    canon = make_track(session, "S", "A")
    twin = make_track(session, "S", "A", album="other")
    _add_file(session, canon, "canon", 128000, is_main=True)
    _add_file(session, twin, "twin", 320000, is_main=True)
    twin.canon_id = canon.id
    session.flush()

    recompute_main_files(session)
    assert recompute_main_files(session) == 0  # nothing left to change


def test_recompute_singleton_marks_its_best_file(session):
    solo = make_track(session, "Solo", "A")
    lo = _add_file(session, solo, "lo", 128000)
    hi = _add_file(session, solo, "hi", 320000)
    session.flush()

    recompute_main_files(session)

    assert hi.is_main is True
    assert lo.is_main is False


def test_auto_deduplicate_recomputes_group_mains(session):
    # Two identical-key tracks become one group; the best file (on whichever
    # track ends up a twin) must own the single is_main flag afterwards.
    a = make_track(session, "Song", "Artist")
    b = make_track(session, "song", "artist")
    fa = _add_file(session, a, "a", 128000)
    fb = _add_file(session, b, "b", 320000)
    session.flush()

    auto_deduplicate(session)

    mains = [f for f in (fa, fb) if f.is_main]
    assert mains == [fb]  # exactly one main, the higher-bitrate copy


# --- the main file of a manually folded group comes from the canon's core ---


def test_main_stays_with_core_when_folded_file_is_better(session):
    """The canon, its auto twin and a manually folded track: the folded 320 kbps file never leads."""
    c = make_track(session, "Song", "Artist", date_added=datetime(2020, 1, 1, tzinfo=UTC))
    auto = make_track(session, "song", "artist", date_added=datetime(2021, 1, 1, tzinfo=UTC))
    f = make_track(session, "Other", "Band", date_added=datetime(2022, 1, 1, tzinfo=UTC))
    fc = _add_file(session, c, "c", 128000)
    fa = _add_file(session, auto, "a", 96000)
    ff = _add_file(session, f, "f", 320000)
    fc.recording_mbid = "11111111-1111-1111-1111-111111111111"
    ff.recording_mbid = "22222222-2222-2222-2222-222222222222"
    add_pair(session, c, f, DecisionKind.SAME)

    auto_deduplicate(session)

    assert f.canon_id == c.id and f.canon_folded is True
    assert auto.canon_id == c.id and auto.canon_folded is False
    assert [x for x in (fc, fa, ff) if x.is_main] == [fc]


def test_main_falls_back_when_core_has_no_file(session):
    """A canon with no file: the folded twin's file is the group's main, so the group still plays."""
    c = make_track(session, "Song", "Artist")
    f = make_track(session, "Other", "Band", canon_id=c.id, canon_folded=True)
    ff = _add_file(session, f, "f", 320000)

    recompute_main_files(session)

    assert ff.is_main is True


def test_group_without_fold_picks_best_anywhere(session):
    """No `canon_folded` anywhere: the core is the whole group, so the best file wins as before."""
    c = make_track(session, "Song", "Artist")
    t = make_track(session, "Song2", "Artist", canon_id=c.id)
    lo = _add_file(session, c, "lo", 128000)
    hi = _add_file(session, t, "hi", 320000)

    recompute_main_files(session)

    assert (lo.is_main, hi.is_main) == (False, True)


def test_main_candidates_are_core_files_or_everything(session):
    """`main_candidates` is the core's files, or every member's when the core owns none."""
    c = make_track(session, "Song", "Artist")
    f = make_track(session, "Other", "Band", canon_id=c.id, canon_folded=True)
    ff = _add_file(session, f, "f", 320000)
    session.refresh(c)
    group = TrackGroup.of(c)
    assert group.core_members == [c]
    assert group.main_candidates() == [ff]
    fc = _add_file(session, c, "c", 128000)
    session.refresh(c)
    assert TrackGroup.of(c).main_candidates() == [fc]
