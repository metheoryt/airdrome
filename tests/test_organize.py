from pathlib import Path

import pytest

from airdrome.library import MAIN_SUBDIR, MUSIC_SUBDIR
from airdrome.library.organize import FileOrganizer
from airdrome.models import Track, TrackFile


def _make_track_with_file(s, tmp_path: Path, title: str, ext: str = "mp3") -> tuple[Track, TrackFile]:
    src = tmp_path / f"{title}.{ext}"
    src.write_bytes(b"\x00" * 100)

    track = Track(title=title, artist="Test Artist", album="Test Album")
    s.add(track)
    s.flush()

    tf = TrackFile(source_path=src, track_id=track.id, bitrate=320000)
    s.add(tf)
    s.flush()

    return track, tf


def test_organize_moves_main_file(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, tf = _make_track_with_file(session, src_dir, "Test Song")
    organizer = FileOrganizer(dst_dir=dst_dir)

    n = organizer.organize(session)

    assert n == 1
    session.refresh(tf)
    assert tf.library_path is not None
    assert tf.is_main is True
    assert (dst_dir / tf.library_path).exists()
    assert not tf.source_path.exists()  # source was moved


def test_organize_copy_mode_keeps_source(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, tf = _make_track_with_file(session, src_dir, "Test Song")
    organizer = FileOrganizer(dst_dir=dst_dir, copy=True)

    organizer.organize(session)

    assert tf.source_path.exists()  # source preserved in copy mode


def test_organize_skips_already_organized(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, _tf = _make_track_with_file(session, src_dir, "Test Song")
    organizer = FileOrganizer(dst_dir=dst_dir)

    n_first = organizer.organize(session)
    n_second = organizer.organize(session)

    assert n_first == 1
    assert n_second == 0  # already organized, nothing to do


def test_dry_run_touches_no_files_but_still_reports(session, tmp_path):
    """A dry run must plan, not act: nothing moves, and the count is still real."""
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, tf = _make_track_with_file(session, src_dir, "Test Song")
    src_before = tf.source_path
    organizer = FileOrganizer(dst_dir=dst_dir, dry_run=True)

    n = organizer.organize(session)

    assert n == 1  # reported as it would have happened
    assert src_before.exists()  # source untouched...
    assert tf.library_path is not None
    assert not (dst_dir / tf.library_path).exists()  # ...and nothing written
    assert list(dst_dir.iterdir()) == []  # not even the destination directories


def test_dry_run_still_raises_on_a_missing_source(session, tmp_path):
    """Preconditions are the point of a dry run — it must not swallow them."""
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, tf = _make_track_with_file(session, src_dir, "Test Song")
    tf.source_path.unlink()
    organizer = FileOrganizer(dst_dir=dst_dir, dry_run=True)

    with pytest.raises(FileNotFoundError):
        organizer.organize(session)


def test_dry_run_detects_a_destination_collision(session, tmp_path):
    """A file already sitting at the destination is reported, not silently ignored."""
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    track, tf = _make_track_with_file(session, src_dir, "Test Song")
    dst_rel = Path(MAIN_SUBDIR) / MUSIC_SUBDIR / track.generate_relative_path(ext="mp3")
    (dst_dir / dst_rel).parent.mkdir(parents=True)
    (dst_dir / dst_rel).write_bytes(b"squatter")
    organizer = FileOrganizer(dst_dir=dst_dir, dry_run=True)

    with pytest.raises(FileExistsError):
        organizer.organize(session)

    assert tf.source_path.exists()


def test_dry_run_plans_the_same_path_a_real_run_would_write(session, tmp_path):
    """The planned `library_path` is the real one, so the preview is worth reading."""
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _track, tf = _make_track_with_file(session, src_dir, "Test Song")
    FileOrganizer(dst_dir=dst_dir, dry_run=True).organize(session)
    planned = tf.library_path

    tf.library_path = None  # same track, same organizer settings, for real this time
    FileOrganizer(dst_dir=dst_dir).organize(session)

    assert tf.library_path == planned
    assert (dst_dir / planned).exists()


def test_on_item_callback_called(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    _make_track_with_file(session, src_dir, "Song A")
    _make_track_with_file(session, src_dir, "Song B")

    calls = []
    organizer = FileOrganizer(dst_dir=dst_dir)
    organizer.organize(session, _on_item=lambda i: calls.append(i))

    assert len(calls) == 2
