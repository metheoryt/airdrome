import shutil
from pathlib import Path

from mutagen import File
from sqlmodel import Session, select

from jellyfist.models import Track, engine
from .schemas import TrackSchema


def select_file(src_dir: Path, paths: set[Path]) -> Path:
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
        f = File(src_dir / p)
        kbps = f.info.bitrate // 1000
        ext = p.suffix[1:]
        priority_path[(kbps, ext_priority[ext])] = p  # don't care if overwrite the key

    key = sorted(priority_path, reverse=True)[0]
    return priority_path[key]


def get_source_copy_paths(ts: TrackSchema, source_dir: Path) -> tuple[Path | None, list[Path]]:
    possible_paths = {p for p in ts.possible_locations(max_suffix=2)}
    existing_paths = set()
    for p in possible_paths:
        path = source_dir / p
        if path.exists():
            existing_paths.add(path)

    if len(existing_paths) == 0:
        print("no files found:", ts.short_info)
        # for p in existing_paths:
        #     print(p)
        return None, []

    if len(existing_paths) > 1:
        src_path = select_file(source_dir, existing_paths)
        copy_paths = [p for p in existing_paths if p != src_path]
        print("multiple files found:", ts.short_info)
        for p in existing_paths:
            print("V" if p == src_path else " ", p)
    else:
        src_path = existing_paths.pop()
        copy_paths = []

    return src_path, copy_paths


def get_target_path(ts: TrackSchema, src_path: Path):
    """
    Use `generate_location` to get a normalized and unified relative destination path for a track.

    This preserves backwards compatibility if the track is moved back and forth.
    """
    ext = src_path.suffix[1:]
    return ts.generate_location(ext=ext, include_disc_num=True, name_limit=35)


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

    moving: set[tuple[Path, Path]] = set()

    with Session(engine) as s:
        for track in s.exec(select(Track).where(Track.apple_music == False)):
            ts = TrackSchema(**track.model_dump())

            src_path, copy_paths = get_source_copy_paths(ts, source_dir)
            if not src_path:
                # no files found, skip
                continue

            # normalized and unified relative destination path for a track
            dst_path = get_target_path(ts, src_path)
            # save it to the database
            track.path = dst_path.as_posix()
            s.flush()

            # main track
            src = source_dir / src_path
            dst = target_dir_originals / dst_path
            if dst.exists():
                print(f"target main file already exist:", dst)
                return
            pair = (src, dst)
            if pair in moving:
                print("main file duplicate move:", pair)
                return
            moving.add(pair)

            # copy paths
            for path in copy_paths:
                src = source_dir / path
                dst = target_dir_copies / dst_path
                if dst.exists():
                    print(f"target copy file already exist:", dst)
                    return
                pair = (src, dst)
                if pair in moving:
                    print("copy file duplicate move:", pair)
                    return
                moving.add(pair)

        for i, (src, dst) in enumerate(moving):
            # create all parent directories for a file
            dst.parent.mkdir(parents=True, exist_ok=True)
            # move the file
            shutil.move(src, dst)
            print(i, "file moved", end="\r", flush=True)

        print()
        print("committing...")
        s.commit()
        print("Done! Make sure you have synced the new library, and come back to sync the playlists")
