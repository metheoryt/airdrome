import shutil
from pathlib import Path
from typing import Callable

from sqlmodel import Session, func, select, update

from airdrome.console import console, make_progress
from airdrome.library import COPIES_SUBDIR, MAIN_SUBDIR, MUSIC_SUBDIR
from airdrome.models import Track, TrackFile, engine


class FileOrganizer:
    def __init__(self, dst_dir: Path, copy: bool = False):
        self.dst_dir = dst_dir
        self.copy = copy

    @classmethod
    def select_main(cls, files: list[TrackFile]) -> TrackFile:
        """
        Select the most suitable file for a track.

        Look for higher kbps.
        """
        # for the same bitrate, prefer m4a over mp3
        ext_priority = {
            "flac": 3,
            "m4a": 2,
            "mp3": 1,
        }

        selected = sorted(
            files,
            key=lambda tf: (tf.bitrate // 1000, ext_priority[tf.source_path.suffix[1:].lower()]),
            reverse=True,
        )[0]
        return selected

    def split_main_copies(self, files: list[TrackFile]) -> tuple[TrackFile | None, list[TrackFile]]:
        if not len(files):
            return None, []

        if len(files) > 1:
            main_tf = self.select_main(files)
            copies = [tf for tf in files if tf.id != main_tf.id]
            console.print("[dim]multiple files — picking best:[/dim]")
            for tf in files:
                marker = "[green]✓[/green]" if tf.id == main_tf.id else " "
                console.print(f"[dim]  {marker} {tf.source_path}[/dim]")
        else:
            main_tf, copies = files[0], []

        main_tf.is_main = True  # mark the main file
        return main_tf, copies

    def transfer(self, src_abs: Path, dst_abs: Path) -> Path | None:
        """
        Move a file from `src_abs` to `dst_dir_mains/dst_rel`.

        Return the real absolut path of the moved destination file.
        """

        if not src_abs.exists():
            # source file does not exist, ignore
            raise FileNotFoundError(f"Source file does not exist: {src_abs}")

        if dst_abs.exists():
            raise FileExistsError(f"Destination file already exists: {dst_abs}")

        dst_abs.parent.mkdir(parents=True, exist_ok=True)

        if self.copy:
            new = shutil.copy(src_abs, dst_abs)
        else:
            new = shutil.move(src_abs, dst_abs)
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

        files = [tf for tf in t.files]

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

    def organize(self, s: Session, reset: bool = False, _on_item: Callable[[int], None] | None = None) -> int:
        """
        Core organize logic. Returns number of tracks transferred.

        Testable directly — no session creation, no progress output.
        """
        if reset:
            s.exec(update(TrackFile).values(library_path=None, is_main=False))
            s.flush()

        pending_stmt = select(Track).where(Track.files.any(TrackFile.library_path.is_(None)))
        i = 0
        for track in s.exec(pending_stmt.order_by(Track.artist_norm, Track.album_norm, Track.title_norm)):
            new_path = self.transfer_track(track)
            if new_path:
                i += 1
                if i % 100 == 0:
                    s.flush()
            if _on_item:
                _on_item(i)

        s.commit()
        return i


def organize_library(
    dst_dir: Path,
    copy: bool = False,
    reset: bool = False,
):
    mover = FileOrganizer(dst_dir=dst_dir, copy=copy)
    verb = "copied" if copy else "moved"
    with Session(engine) as s:
        if reset:
            console.print("[yellow]library paths reset[/yellow]")

        pending_stmt = select(Track).where(Track.files.any(TrackFile.library_path.is_(None)))
        total = s.exec(select(func.count()).select_from(pending_stmt.subquery())).one()
        if not total and not reset:
            console.print("[dim]Nothing to do.[/dim]")
            return

        with make_progress() as progress:
            task = progress.add_task(f"Organizing library ({verb})", total=total)
            i = mover.organize(s, reset=reset, _on_item=lambda _: progress.advance(task))

    console.print(f"[green]{i} tracks {verb}[/green]")
