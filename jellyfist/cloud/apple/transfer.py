import shutil
from pathlib import Path

from sqlmodel import Session, select

from jellyfist.models import Track, engine
from .schemas import TrackSchema


def transfer_library(source_dir: Path, target_dir: Path):
    if not source_dir.is_dir():
        print("source directory does not exist:", source_dir)
        return
    if not target_dir.is_dir():
        p = input(f"target directory {target_dir} does not exist, create? [yN]: ")
        if p.strip().lower() != "y":
            print("abort")
            return
        target_dir.mkdir(parents=True, exist_ok=True)
        print("target directory created:", target_dir)

    moves = set()

    with Session(engine) as s:
        for track in s.exec(select(Track).where(Track.files.any())):
            # track_data = {k: getattr(track, k) for k in Track.model_fields}
            ts = TrackSchema(**track.model_dump())
            possible_paths = list(ts.possible_locations(max_suffix=2))
            existing_paths = [Path(tf.path) for tf in track.files]

            # sort paths in order of priority (mp3 over m4a)
            # there should be at least 1
            relative_paths = [p for p in possible_paths if p in existing_paths]
            if not relative_paths:
                print("no match between possible locations and existing files:")
                print("possible:", possible_paths)
                print("existing", existing_paths)
                return

            relative_path = relative_paths[0]
            src = source_dir / relative_path
            if not src.exists():
                print(f"source file does not exist:", src)
                return

            dst = target_dir / relative_path
            if dst.exists():
                print(f"target file already exists:", dst)
                return

            if relative_path in moves:
                print(f"duplicate path found:", relative_path)
                return

            moves.add(relative_path)

    if not moves:
        print("no files to move")
        return

    # only move files after duplicate checks passed
    print("moving", len(moves), "files...")
    for relative_path in moves:
        src = source_dir / relative_path
        dst = target_dir / relative_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        # move the file
        shutil.move(src, dst)
    print("Done! Make sure you have synced the new library, and come back to sync the playlists")
