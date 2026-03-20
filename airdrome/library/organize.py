import shutil
from pathlib import Path

from sqlmodel import Session, select

from airdrome.models import Track, TrackFile, engine


class FileOrganizer:
    def __init__(self, dst_dir: Path, dst_dir_copies: Path, copy: bool = False):
        self.dst_dir = dst_dir
        self.dst_dir_copies = dst_dir_copies
        self.copy = copy

    def select_file(self, files: list[TrackFile]) -> TrackFile:
        """
        Select the most suitable file for a track.

        Look for higher kbps.
        """
        # for the same bitrate, prefer m4a over mp3
        ext_priority = {
            "m4a": 2,
            "mp3": 1,
        }

        selected = sorted(
            files,
            key=lambda tf: (tf.bitrate // 1000, ext_priority[tf.path.suffix[1:].lower()]),
            reverse=True,
        )[0]
        return selected

    def get_file_paths(self, files: list[TrackFile]) -> tuple[TrackFile | None, list[TrackFile]]:
        if not len(files):
            return None, []

        if len(files) > 1:
            canon_tf = self.select_file(files)
            copies = [tf for tf in files if tf.id != canon_tf.id]
            print("multiple files found:")
            for tf in files:
                print("V" if tf.id == canon_tf.id else " ", tf.path)
            # input("Press enter to continue...")
        else:
            canon_tf = files[0]
            copies = []

        return canon_tf, copies

    def transfer(self, src_abs: Path, dst_rel: Path, dst_base: Path) -> Path:
        """
        Move a file from `src_abs` to `dst_dir/dst_rel`.

        Return the real relative path of the moved destination file.
        """
        dst_abs = dst_base / dst_rel

        if dst_abs.exists():
            return dst_abs.resolve()
            # raise FileExistsError(f"Destination file already exists: {dst}")
        dst_abs.parent.mkdir(parents=True, exist_ok=True)

        if self.copy:
            new = shutil.copy(src_abs, dst_abs)
        else:
            new = shutil.move(src_abs, dst_abs)

        new = Path(new).resolve()  # get actual path
        return new

    def transfer_track(self, t: Track) -> Path | None:
        """
        Transfer track files to destination directories.

        The main track file is transferred to the `target_dir` directory.
        Other files that also represent the track are transferred to the `target_dir_copies` directory.

        :return: The relative path of the transferred main track file.
        :return: None, if no transfer happened.
        """
        if t.canon:
            # Do not handle twins
            return None

        files = [tf for tf in t.files]

        if t.twins:
            # the track has twins, combine all files from all twins
            files.extend([tf for t in t.twins for tf in t.files])

        if t.main_path:
            dst = self.dst_dir / t.main_path
            if dst.exists():
                # already transferred, don't even check for copies
                return None

        if not len(files):
            return None

        canon, copies = self.get_file_paths(files)
        ext = canon.path.suffix[1:]
        new_rel_path = self.transfer(
            src_abs=canon.path, dst_rel=t.generate_main_path(ext), dst_base=self.dst_dir
        )

        # copies
        for i, tf in enumerate(copies):
            ext = tf.path.suffix[1:]
            rel_path = t.generate_main_path(ext, suffix=i)
            try:
                self.transfer(
                    src_abs=tf.path, dst_rel=t.generate_main_path(ext, suffix=i), dst_base=self.dst_dir_copies
                )
            except FileExistsError:
                # ignore existing copyfile error
                print("copy file already exist:", self.dst_dir_copies / rel_path)

        return new_rel_path


def organize_library(
    target_dir_originals: Path,
    target_dir_copies: Path,
    copy: bool = False,
):
    for path in (target_dir_originals, target_dir_copies):
        if not path.is_dir():
            path.mkdir(parents=True)

    mover = FileOrganizer(dst_dir=target_dir_originals, dst_dir_copies=target_dir_copies, copy=copy)
    i = 0
    with Session(engine) as s:
        for track in s.exec(select(Track).where(Track.main_path.isnot(None))):
            track: Track
            new_path = mover.transfer_track(track)
            if new_path:
                i += 1
                track.main_path = new_path.as_posix()
                if i % 100 == 0:
                    s.flush()

                print(i, "tracks moved", end="\r", flush=True)

        print()
        print("committing...")
        s.commit()
        print("Done!")
