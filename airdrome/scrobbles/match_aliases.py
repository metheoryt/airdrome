from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, select, update

from airdrome.match import find_best_track
from airdrome.models import TrackAlias, engine


progress = Progress(
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("✅ {task.fields[match]}  "),
    TextColumn("⚠️ {task.fields[multimatch]}  "),
    TextColumn("❌ {task.fields[mismatch]}  "),
    TimeElapsedColumn(),
)


def match_aliases(reset: bool = False, dry_run: bool = False, threshold: float = 0.4):
    with Session(engine) as s, progress:
        if reset:
            s.exec(update(TrackAlias).values(track_id=None))
            s.flush()
            print("dropped all alias-track links")

        aliases = s.exec(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()

        match = mismatch = multimatch = 0
        task_id = progress.add_task(
            f"Matching {len(aliases)} aliases{' [dry run]' if dry_run else ''}",
            total=len(aliases),
            match=match,
            mismatch=mismatch,
            multimatch=multimatch,
        )
        for alias in aliases:
            alias: TrackAlias

            track = find_best_track(
                s, alias.title_norm, alias.artist_norm, alias.album_norm, threshold=threshold
            )
            if track:
                match += 1
                alias.track = track
                if match % 100 == 0:
                    s.flush()
            else:
                mismatch += 1

            progress.update(
                task_id,
                advance=1,
                match=match,
                mismatch=mismatch,
                multimatch=multimatch,
            )

        if dry_run:
            s.rollback()
            print("dry run, no changes were made")
        else:
            s.commit()
            print("changes committed")
