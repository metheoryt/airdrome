import shutil
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.console import console, detail, done, make_progress
from airdrome.library import COPIES_SUBDIR, MAIN_SUBDIR, MUSIC_SUBDIR
from airdrome.models import Track, TrackFile, TrackGroup


class FileOrganizer:
    """Places bound files under `dst_dir`, one destination per track.

    Under `dry_run` nothing on disk is touched — no directories created, no bytes
    moved or copied — but everything else runs: preconditions are still checked and
    `TrackFile.library_path` is still written, so the reported count and paths are the
    ones a real run would produce. Undoing those DB writes is the caller's job (the CLI
    rolls the session back). See `transfer` for what a dry run cannot see.
    """

    def __init__(self, dst_dir: Path, copy: bool = False, dry_run: bool = False) -> None:
        self.dst_dir = dst_dir
        self.copy = copy
        self.dry_run = dry_run

    @classmethod
    def select_main(cls, files: list[TrackFile]) -> TrackFile:
        """Select the most suitable file for a track (highest bitrate, then container)."""
        return TrackGroup.select_main_file(files)

    def split_main_copies(self, files: list[TrackFile]) -> tuple[TrackFile | None, list[TrackFile]]:
        if not len(files):
            return None, []

        if len(files) > 1:
            main_tf = self.select_main(files)
            copies = [tf for tf in files if tf.id != main_tf.id]
            # Per-file detail is verbose-only: on a large library this fires for many tracks and
            # would bury the progress bar. The chosen main is recorded on disk regardless.
            detail("[dim]multiple files — picking best:[/dim]")
            for tf in files:
                marker = "[green]✓[/green]" if tf.id == main_tf.id else " "
                detail(f"[dim]  {marker} {tf.source_path}[/dim]")
        else:
            main_tf, copies = files[0], []

        # Set the flag authoritatively over the group's files so a re-organize
        # after a dedup change can never leave two files marked main.
        main_tf.is_main = True
        for tf in copies:
            tf.is_main = False
        return main_tf, copies

    def transfer(self, src_abs: Path, dst_abs: Path) -> Path | None:
        """
        Move a file from `src_abs` to `dst_dir_mains/dst_rel`.

        Return the real absolut path of the moved destination file.

        Both preconditions are checked even under `dry_run` — surfacing a missing source
        or an occupied destination is the point of a dry run. What a dry run *cannot*
        see is a collision between two tracks that resolve to the same destination: in a
        real run the second one hits the first's file, but nothing is written here, so
        the pair reads as clean. Detecting that would need planned paths tracked across
        calls.
        """

        if not src_abs.exists():
            # source file does not exist, ignore
            raise FileNotFoundError(f"Source file does not exist: {src_abs}")

        if dst_abs.exists():
            raise FileExistsError(f"Destination file already exists: {dst_abs}")

        if self.dry_run:
            # Report the path a real run would produce, without creating anything.
            return dst_abs

        dst_abs.parent.mkdir(parents=True, exist_ok=True)

        new = shutil.copy(src_abs, dst_abs) if self.copy else shutil.move(src_abs, dst_abs)
        # return a real path, with the correct case
        return new.resolve()

    def transfer_file(self, tf: TrackFile, dst_rel: Path) -> Path | None:
        """
        Transfer a single file to a destination directory.

        Write the relative library path to the TrackFile instance library path.
        Return the relative path of the transferred file if it was transferred, None otherwise.
        """
        if tf.library_path and (self.dst_dir / tf.library_path).exists():
            return None

        dst_abs_real = self.transfer(
            src_abs=tf.source_path,
            dst_abs=self.dst_dir / dst_rel,
        )
        dst_rel_real = dst_abs_real.relative_to(self.dst_dir)
        tf.library_path = dst_rel_real
        return dst_rel_real

    def transfer_track(self, t: Track) -> Path | None:
        """
        Transfer track files to destination directories.

        The main track file is transferred to the library directory.
        Other files that also represent the track are transferred to the library copies directory.

        :return: The relative path of the transferred main track file.
        :return: None, if no transfer happened.
        """
        if t.canon:
            # Do not handle twins, they will be handled together with their canon track
            return self.transfer_track(t.canon)

        files = list(t.files)

        if t.twins:
            # the track has twins, combine all files from all twins
            files.extend([tf for t in t.twins for tf in t.files])

        if not len(files):
            return None

        main_tf, copies = self.split_main_copies(files)

        # main file
        dst_rel = t.generate_relative_path(ext=main_tf.source_path.suffix[1:])
        new_path = self.transfer_file(main_tf, dst_rel=Path(MAIN_SUBDIR) / MUSIC_SUBDIR / dst_rel)
        if not new_path:
            return None

        # copies
        for i, copy_tf in enumerate(copies):
            dst_rel = t.generate_relative_path(ext=copy_tf.source_path.suffix[1:], suffix=i)
            self.transfer_file(copy_tf, dst_rel=Path(COPIES_SUBDIR) / MUSIC_SUBDIR / dst_rel)
        return new_path

    def organize(self, s: Session, _on_item: Callable[[int], None] | None = None) -> int:
        """
        Core organize logic. Returns number of tracks transferred.

        Testable directly — no session creation, no progress output.
        """
        pending_stmt = select(Track).where(Track.files.any(TrackFile.library_path.is_(None)))
        i = 0
        for track in s.scalars(pending_stmt.order_by(Track.artist_norm, Track.album_norm, Track.title_norm)):
            new_path = self.transfer_track(track)
            if new_path:
                i += 1
                if i % 100 == 0:
                    s.flush()
            if _on_item:
                _on_item(i)

        s.flush()
        return i


def organize_library(
    s: Session,
    dst_dir: Path,
    copy: bool = False,
    dry_run: bool = False,
) -> None:
    mover = FileOrganizer(dst_dir=dst_dir, copy=copy, dry_run=dry_run)
    # Under a dry run nothing is written, so the summary must not claim it was.
    label = ("copying" if copy else "moving") if dry_run else ("copied" if copy else "moved")
    outcome = f"would be {'copied' if copy else 'moved'}" if dry_run else label

    pending_stmt = select(Track).where(Track.files.any(TrackFile.library_path.is_(None)))
    total = s.scalars(select(func.count()).select_from(pending_stmt.subquery())).one()
    if not total:
        console.print("[dim]Nothing to do.[/dim]")
        return

    scope = f"dry run, {label}" if dry_run else label
    with make_progress() as progress:
        task = progress.add_task(f"Organizing library ({scope})", total=total)
        i = mover.organize(s, _on_item=lambda _: progress.advance(task))

    done(f"{i} tracks {outcome}")
