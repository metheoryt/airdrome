import shutil
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

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

    def split_main_copies(
        self, files: list[TrackFile], candidates: list[TrackFile] | None = None
    ) -> tuple[TrackFile | None, list[TrackFile]]:
        """Split `files` into the main and the copies; the main is picked among `candidates` (default all)."""
        if not len(files):
            return None, []

        if len(files) > 1:
            main_tf = self.select_main(files if candidates is None else candidates)
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

    def transfer(
        self, src_abs: Path, dst_abs: Path, *, copy: bool | None = None, vacated: bool = False
    ) -> Path:
        """
        Move a file from `src_abs` to `dst_dir_mains/dst_rel`.

        `copy` overrides the organizer's mode for this one file (None keeps it). `vacated` says
        `dst_abs` is freed earlier in the same operation: a real run has already emptied it, while a
        dry run still sees the old file there and must not report that as a collision.

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

        if dst_abs.exists() and not (self.dry_run and vacated):
            raise FileExistsError(f"Destination file already exists: {dst_abs}")

        if self.dry_run:
            # Report the path a real run would produce, without creating anything.
            return dst_abs

        dst_abs.parent.mkdir(parents=True, exist_ok=True)

        copy = self.copy if copy is None else copy
        new = shutil.copy(src_abs, dst_abs) if copy else shutil.move(src_abs, dst_abs)
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

        :return: The relative path of the transferred main track file. When the main was already placed
            and only manually folded files (outside the main candidates) were placed into Copies/, the
            main's existing path. None, if no transfer happened.
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

        # The main comes from the canon's core, so a manually folded file never displaces it.
        candidates = TrackGroup.of(t).main_candidates()
        placed = self._placed_main(files)
        if placed is not None:
            best = self.select_main(candidates)
            if best is not placed and best.library_path is None:
                # An upgrade arrived for a group already in Library/: swap it in rather than collide.
                self.swap_upgrade(placed, best, t)
                for copy_tf in files:
                    if copy_tf.library_path is None:
                        ext = copy_tf.source_path.suffix[1:]
                        self.transfer_file(copy_tf, dst_rel=self._free_copy_rel(t, ext, files))
                return best.library_path

        main_tf, copies = self.split_main_copies(files, candidates)

        # main file
        dst_rel = t.generate_relative_path(ext=main_tf.source_path.suffix[1:])
        new_path = self.transfer_file(main_tf, dst_rel=Path(MAIN_SUBDIR) / MUSIC_SUBDIR / dst_rel)
        if not new_path:
            # The main already sits on disk. Only files folded in manually (outside the main
            # candidates) that have no place yet go to Copies/; any other unplaced file is left alone.
            pending = [
                tf for tf in copies if tf.library_path is None and not any(tf is c for c in candidates)
            ]
            for copy_tf in pending:
                ext = copy_tf.source_path.suffix[1:]
                self.transfer_file(copy_tf, dst_rel=self._free_copy_rel(t, ext, files))
            return main_tf.library_path if pending else None

        # copies
        for i, copy_tf in enumerate(copies):
            dst_rel = t.generate_relative_path(ext=copy_tf.source_path.suffix[1:], suffix=i)
            self.transfer_file(copy_tf, dst_rel=Path(COPIES_SUBDIR) / MUSIC_SUBDIR / dst_rel)
        return new_path

    @staticmethod
    def _placed_main(files: list[TrackFile]) -> TrackFile | None:
        """The group's file already sitting in Library/, preferring the one flagged `is_main`.

        Keyed on placement rather than the flag alone: dedup's main recompute may already have moved
        `is_main` onto an unplaced upgrade, and Library/ is what the swap has to free.
        """
        in_library = [f for f in files if f.library_path and f.library_path.parts[0] == MAIN_SUBDIR]
        return next((f for f in in_library if f.is_main), in_library[0] if in_library else None)

    def _free_copy_rel(self, t: Track, ext: str, files: list[TrackFile]) -> Path:
        """The first Copies/ path for `t` (suffix 0, 1, …) that is neither on disk nor planned for a file.

        Checking the group's `library_path`s as well as the disk keeps a dry run, which writes nothing,
        from planning two files onto one path.
        """
        taken = {f.library_path for f in files if f.library_path}
        suffix = 0
        while True:
            rel = Path(COPIES_SUBDIR) / MUSIC_SUBDIR / t.generate_relative_path(ext=ext, suffix=suffix)
            if rel not in taken and not (self.dst_dir / rel).exists():
                return rel
            suffix += 1

    def swap_upgrade(self, main: TrackFile, new: TrackFile, t: Track) -> None:
        """Put the upgrade `new` into `main`'s Library/ path and move `main` to the next free Copies/ path.

        The old main is always *moved*, whatever the copy mode: it already lives inside the library, and
        copying would leave it squatting on the path the upgrade needs. No library file is ever deleted;
        a failed transfer removes only its own partial upgrade (`_undo_swap`). Under `dry_run` only the
        planned paths and flags are written (the caller rolls the session back).
        """
        files = [f for m in TrackGroup.of(t).members for f in m.files]
        freed = main.library_path
        assert freed is not None  # callers pass a placed main
        target = self.dst_dir / freed.with_suffix(new.source_path.suffix)
        vacated = target == self.dst_dir / freed
        # Check the upgrade's side before touching the old main, so a failure leaves Library/ as it was.
        if not new.source_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {new.source_path}")
        if not vacated and target.exists():
            raise FileExistsError(f"Destination file already exists: {target}")

        copy_rel = self._free_copy_rel(t, ext=freed.suffix[1:], files=files)
        moved = self.transfer(self.dst_dir / freed, self.dst_dir / copy_rel, copy=False)
        parked = moved.relative_to(self.dst_dir)
        main.library_path = parked
        # library_path is unique: the old row must give up `freed` in the DB before the new one takes it,
        # and the unit of work does not order the two UPDATEs for us.
        if (s := object_session(main)) is not None:
            s.flush()

        # The freed path keeps its name; only the extension follows the upgrade's container.
        try:
            new_abs = self.transfer(new.source_path, target, vacated=vacated)
        except Exception:
            # Exception, not BaseException: a Ctrl-C mid-move must not trigger a second move.
            self._undo_swap(main, new, files, freed, parked, target, vacated=vacated)
            if s is not None:
                s.flush()
            raise
        new.library_path = new_abs.relative_to(self.dst_dir)
        for f in files:
            f.is_main = f is new

    def _undo_swap(
        self,
        main: TrackFile,
        new: TrackFile,
        files: list[TrackFile],
        freed: Path,
        parked: Path,
        target: Path,
        *,
        vacated: bool,
    ) -> None:
        """After a failed upgrade transfer, make disk and DB agree again without overwriting any file.

        The upgrade already landed at `target` and its source is gone (a `--move` that failed late):
        moving the old main back would overwrite the only copy, so both files stay put and the DB
        records the upgrade as the placed main. Otherwise a partial upgrade at a `target` other than
        `freed` (new extension) is removed first, since its source is intact. Then, when `freed` is
        empty or the upgrade's source still exists (so nothing unique sits at `freed`), the old main
        goes back; anything else is left where it is, with the old main recorded at its parked path.
        """
        if self.dry_run:
            main.library_path = freed  # nothing moved on disk; only the plan is undone
            return
        if target.exists() and not new.source_path.exists():
            new.library_path = target.relative_to(self.dst_dir)
            for f in files:
                f.is_main = f is new
            return
        if not vacated and target.exists():
            # A partial upgrade (a copy, or a cross-filesystem move, that died midway) under its own
            # extension. Safe to drop: its source is intact (else the branch above), and `swap_upgrade`
            # refused to start when `target` already existed, so it is nobody else's file.
            target.unlink()
        if not (self.dst_dir / freed).exists() or new.source_path.exists():
            shutil.move(self.dst_dir / parked, self.dst_dir / freed)
            main.library_path = freed

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
