import shutil
from pathlib import Path

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

    track, tf = _make_track_with_file(session, src_dir, "Test Song")
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

    track, tf = _make_track_with_file(session, src_dir, "Test Song")
    organizer = FileOrganizer(dst_dir=dst_dir, copy=True)

    organizer.organize(session)

    assert tf.source_path.exists()  # source preserved in copy mode


def test_organize_skips_already_organized(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    track, tf = _make_track_with_file(session, src_dir, "Test Song")
    organizer = FileOrganizer(dst_dir=dst_dir)

    n_first = organizer.organize(session)
    n_second = organizer.organize(session)

    assert n_first == 1
    assert n_second == 0  # already organized, nothing to do


def test_organize_reset_re_organizes(session, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "library"
    src_dir.mkdir()
    dst_dir.mkdir()

    track, tf = _make_track_with_file(session, src_dir, "Test Song", ext="mp3")
    organizer = FileOrganizer(dst_dir=dst_dir)
    organizer.organize(session)

    session.refresh(tf)
    old_path = tf.library_path

    # move the file back to source for re-organization
    shutil.move(dst_dir / old_path, tf.source_path)

    n = organizer.organize(session, reset=True)

    assert n == 1


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
