import shutil
from pathlib import Path

from sqlmodel import Session, select

from jellyfist.models import Track, engine
from .schemas import TrackSchema


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

    originals = set()
    copies = set()

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

            # 1 original, rest copies
            original_paths, copy_paths = relative_paths[:1], relative_paths[1:]

            # make copy paths unique and remove the original from them
            copy_paths = set(copy_paths)
            copy_paths.discard(original_paths[0])

            for cat_name, category, paths in (
                ("originals", originals, original_paths),
                ("copies", copies, copy_paths),
            ):
                for path in paths:
                    src = source_dir / path
                    if not src.exists():
                        # print(f"source file does not exist:", src)
                        # return
                        continue

                    dst = target_dir_originals / path
                    if dst.exists():
                        print(f"source and target files already exist:", path)
                        return

                    if path in category:
                        print(f"duplicate path found in {cat_name}:", path)
                        return

                    category.add(path)

        for cat_name, category, target_dir in (
            ("originals", originals, target_dir_originals),
            ("copies", copies, target_dir_copies),
        ):
            if not category:
                print("no files to move in category:", cat_name)
                continue

            # only move files after duplicate checks passed
            print("moving", len(category), f"{cat_name}...")
            for relative_path in category:
                src = source_dir / relative_path
                dst = target_dir / relative_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                # move the file
                shutil.move(src, dst)

        print("Done! Make sure you have synced the new library, and come back to sync the playlists")
