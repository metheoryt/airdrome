from pathlib import Path

import pytest

from airdrome.library import COPIES_SUBDIR, MAIN_SUBDIR, MUSIC_SUBDIR
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


# ── upgrade swap ────────────────────────────────────────────────────────────────


@pytest.fixture()
def placed_main(session, tmp_path: Path) -> tuple[Track, TrackFile, Path]:
    """A track whose 128 kbps mp3 main already sits in Library/, plus one copy already in Copies/."""
    dst_dir = tmp_path / "library"
    track = Track(title="Song", artist="Artist", album="Album")
    session.add(track)
    session.flush()
    rel = Path(MAIN_SUBDIR) / MUSIC_SUBDIR / track.generate_relative_path(ext="mp3")
    copy_rel = Path(COPIES_SUBDIR) / MUSIC_SUBDIR / track.generate_relative_path(ext="mp3")
    for r, payload in ((rel, b"old main"), (copy_rel, b"old copy")):
        (dst_dir / r).parent.mkdir(parents=True, exist_ok=True)
        (dst_dir / r).write_bytes(payload)
    main = TrackFile(source_path=tmp_path / "gone/a.mp3", library_path=rel, is_main=True, bitrate=128_000)
    copy = TrackFile(source_path=tmp_path / "gone/b.mp3", library_path=copy_rel, bitrate=96_000)
    main.track = copy.track = track
    session.add_all([main, copy])
    session.flush()
    return track, main, dst_dir


def _add_upgrade(session, tmp_path: Path, track: Track, ext: str = "flac") -> TrackFile:
    """An unplaced, higher-bitrate file of `track`, as `import` leaves an upgrade."""
    src = tmp_path / "loose" / f"song.{ext}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"better")
    tf = TrackFile(source_path=src, bitrate=900_000)
    tf.track = track
    session.add(tf)
    session.flush()
    return tf


def test_swap_upgrade_relocates_old_main_to_next_free_copy(session, tmp_path, placed_main) -> None:
    """The upgrade takes the freed Library/ path (its own extension); the old main moves to Copies/."""
    track, main, dst_dir = placed_main
    freed = main.library_path
    new = _add_upgrade(session, tmp_path, track)

    n = FileOrganizer(dst_dir=dst_dir).organize(session)

    assert n == 1
    assert new.library_path == freed.with_suffix(".flac")
    assert (dst_dir / new.library_path).read_bytes() == b"better"
    assert not (dst_dir / freed).exists()
    # Suffix 0 is the existing copy's, so the old main takes the next free one.
    assert main.library_path == Path(COPIES_SUBDIR) / MUSIC_SUBDIR / track.generate_relative_path(
        ext="mp3", suffix=1
    )
    assert (dst_dir / main.library_path).read_bytes() == b"old main"
    assert new.is_main and not main.is_main


def test_swap_in_copy_mode_still_moves_the_old_main(session, tmp_path, placed_main) -> None:
    """Copy mode copies the upgrade in from its folder, but the old main is moved inside the library."""
    track, main, dst_dir = placed_main
    new = _add_upgrade(session, tmp_path, track, ext="mp3")
    freed = main.library_path

    FileOrganizer(dst_dir=dst_dir, copy=True).organize(session)

    assert new.source_path.exists()
    assert new.library_path == freed
    assert (dst_dir / freed).read_bytes() == b"better"
    assert (dst_dir / main.library_path).read_bytes() == b"old main"


def test_dry_run_swap_plans_without_touching_disk(session, tmp_path, placed_main) -> None:
    """Under dry run the swap only plans paths, even when the upgrade wants the very path it frees."""
    track, main, dst_dir = placed_main
    new = _add_upgrade(session, tmp_path, track, ext="mp3")
    freed = main.library_path
    before = sorted(p for p in dst_dir.rglob("*"))

    FileOrganizer(dst_dir=dst_dir, dry_run=True).organize(session)

    assert new.library_path == freed
    assert main.library_path.parts[0] == COPIES_SUBDIR
    assert sorted(p for p in dst_dir.rglob("*")) == before
    assert (dst_dir / freed).read_bytes() == b"old main"
    assert new.source_path.exists()


def test_failed_swap_puts_the_old_main_back(session, tmp_path, placed_main, monkeypatch) -> None:
    """If the upgrade's transfer fails, the old main returns to Library/ on disk and in the DB."""
    track, main, dst_dir = placed_main
    new = _add_upgrade(session, tmp_path, track, ext="mp3")
    freed = main.library_path
    real = FileOrganizer.transfer

    def failing(self, src_abs, dst_abs, **kw):
        if src_abs == new.source_path:
            raise OSError("disk full")
        return real(self, src_abs, dst_abs, **kw)

    monkeypatch.setattr(FileOrganizer, "transfer", failing)

    with pytest.raises(OSError, match="disk full"):
        FileOrganizer(dst_dir=dst_dir).organize(session)

    session.expire_all()
    assert main.library_path == freed and main.is_main
    assert (dst_dir / freed).read_bytes() == b"old main"
    assert new.library_path is None and new.source_path.read_bytes() == b"better"
    assert sorted(p.name for p in (dst_dir / COPIES_SUBDIR).rglob("*.mp3")) == [
        "Song.mp3"
    ]  # only the old copy


def test_failed_swap_never_overwrites_an_upgrade_that_already_landed(
    session, tmp_path, placed_main, monkeypatch
) -> None:
    """A failure after the upgrade was moved into Library/ leaves it there; the old main stays in Copies/."""
    track, main, dst_dir = placed_main
    new = _add_upgrade(session, tmp_path, track, ext="mp3")
    freed = main.library_path
    real = FileOrganizer.transfer

    def lands_then_fails(self, src_abs, dst_abs, **kw):
        result = real(self, src_abs, dst_abs, **kw)
        if src_abs == new.source_path:
            raise OSError("fsync failed")  # the move completed: the source is gone, the file is at freed
        return result

    monkeypatch.setattr(FileOrganizer, "transfer", lands_then_fails)

    with pytest.raises(OSError, match="fsync failed"):
        FileOrganizer(dst_dir=dst_dir).organize(session)

    assert (dst_dir / freed).read_bytes() == b"better"
    assert not new.source_path.exists()
    # The DB follows the disk: the upgrade is the placed main, the old main is parked in Copies/.
    assert new.library_path == freed and new.is_main
    assert main.library_path.parts[0] == COPIES_SUBDIR and not main.is_main
    assert (dst_dir / main.library_path).read_bytes() == b"old main"


def test_failed_cross_extension_swap_removes_the_partial_upgrade(
    session, tmp_path, placed_main, monkeypatch
) -> None:
    """M-3: a copy of a .flac upgrade that dies midway leaves no partial .flac beside the restored mp3."""
    track, main, dst_dir = placed_main
    new = _add_upgrade(session, tmp_path, track, ext="flac")
    freed = main.library_path
    target = dst_dir / freed.with_suffix(".flac")
    real = FileOrganizer.transfer

    def partial_then_fails(self, src_abs, dst_abs, **kw):
        if src_abs == new.source_path:
            dst_abs.write_bytes(b"bet")  # half-written, source untouched
            raise OSError("disk full")
        return real(self, src_abs, dst_abs, **kw)

    monkeypatch.setattr(FileOrganizer, "transfer", partial_then_fails)

    with pytest.raises(OSError, match="disk full"):
        FileOrganizer(dst_dir=dst_dir, copy=True).organize(session)

    assert not target.exists()
    assert main.library_path == freed and (dst_dir / freed).read_bytes() == b"old main"
    assert new.library_path is None and new.source_path.read_bytes() == b"better"
