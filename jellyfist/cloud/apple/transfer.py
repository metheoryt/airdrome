import shutil
from pathlib import Path

from mutagen import File
from sqlmodel import Session, select

from jellyfist.models import Track, engine
from .schemas import TrackSchema


class TrackMover:
    def __init__(self, src_dir: Path, dst_dir: Path, dst_dir_copies: Path):
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.dst_dir_copies = dst_dir_copies

    def select_file(self, paths: set[Path]) -> Path:
        """
        Select the most suitable file for a track.

        Look for higher kbps.
        """

        # look for the higher bitrate first
        priority_path = {}
        # for the same bitrate, prefer m4a over mp3
        ext_priority = {
            "mp3": 1,
            "m4a": 2,
        }
        for p in paths:
            f = File(self.src_dir / p)
            kbps = f.info.bitrate // 1000
            ext = p.suffix[1:]
            priority_path[(kbps, ext_priority[ext])] = p  # don't care if overwrite the key

        key = sorted(priority_path, reverse=True)[0]
        return priority_path[key]

    def get_file_paths(self, ts: TrackSchema) -> tuple[Path | None, list[Path]]:
        possible_paths = {p for p in ts.possible_locations(max_suffix=2)}
        existing_paths = set()
        for p in possible_paths:
            path = self.src_dir / p
            if path.exists():
                existing_paths.add(path)

        if len(existing_paths) == 0:
            print("no files found:", ts.short_info)
            # for p in existing_paths:
            #     print(p)
            return None, []

        if len(existing_paths) > 1:
            src_path = self.select_file(existing_paths)
            copy_paths = [p for p in existing_paths if p != src_path]
            print("multiple files found:", ts.short_info)
            for p in existing_paths:
                print("V" if p == src_path else " ", p)
        else:
            src_path = existing_paths.pop()
            copy_paths = []

        return src_path, copy_paths

    def get_target_path(self, ts: TrackSchema, src_path: Path):
        """
        Use `generate_location` to get a normalized and unified relative destination path for a track.

        This preserves backwards compatibility if the track is moved back and forth.
        """
        ext = src_path.suffix[1:]
        return ts.generate_location(ext=ext, include_disc_num=True, name_limit=35)

    def move_file(self, src: Path, dst_rel: Path, dst_base: Path) -> Path:
        """
        Move a file from `src` to `dst`.

        Return the absolute path of the destination file.
        """
        dst = dst_base / dst_rel
        if dst.exists():
            raise FileExistsError(f"Destination file already exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        new = shutil.move(src, dst)
        new = Path(new).resolve()  # get actual path
        return new.relative_to(dst_base)

    def transfer_track(self, t: Track) -> Path | None:
        """
        Transfer track files to destination directories.

        The main track file is transferred to the `target_dir` directory.
        Other files that also represent the track are transferred to the `target_dir_copies` directory.

        :return: The relative path of the transferred main track file.
        :return: None, if no transfer happened.
        """
        if t.path:
            dst = self.dst_dir / t.path
            if dst.exists():
                # already transferred, don't even check for copies
                return None

        ts = TrackSchema(**t.model_dump())
        src_path, copy_paths = self.get_file_paths(ts)

        if not src_path:
            # no files found
            return None

        dst_path = self.get_target_path(ts, src_path)
        new_rel_path = self.move_file(self.src_dir / src_path, dst_path, self.dst_dir)

        # copies
        for path in copy_paths:
            try:
                self.move_file(self.src_dir / path, path, self.dst_dir_copies)
            except FileExistsError:
                # ignore existing copy error
                print("copy file already exist:", self.dst_dir_copies / path)

        return new_rel_path


def transfer_library(source_dir: Path, target_dir_originals: Path, target_dir_copies: Path):
    if not source_dir.is_dir():
        print("source directory does not exist:", source_dir)
        return

    for path in (target_dir_originals, target_dir_copies):
        if not path.is_dir():
            p = input(f"target directory {path} does not exist, create? [yN]: ")
            if p.strip().lower() != "y":
                print("abort")
                return
            path.mkdir(parents=True, exist_ok=True)
            print("target directory created:", path)

    mover = TrackMover(src_dir=source_dir, dst_dir=target_dir_originals, dst_dir_copies=target_dir_copies)
    i = 0
    with Session(engine) as s:
        for track in s.exec(select(Track).where(Track.apple_music == False)):
            new_path = mover.transfer_track(track)
            if new_path:
                i += 1
                track.path = new_path.as_posix()
                if i % 100 == 0:
                    s.flush()

                print(i, "tracks moved", end="\r", flush=True)

        print()
        print("committing...")
        s.commit()
        print("Done! Make sure you have synced the new library, and come back to sync the playlists")
