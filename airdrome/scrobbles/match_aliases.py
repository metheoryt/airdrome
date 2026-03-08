from collections import Counter

from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn
from sqlmodel import Session, select, update

from airdrome.match import generate_match_filter_sets
from airdrome.models import TrackAlias, Track, engine, TrackFile

progress = Progress(
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("✅ {task.fields[match]}  "),
    TextColumn("⚠️ {task.fields[multimatch]}  "),
    TextColumn("❌ {task.fields[mismatch]}  "),
    TimeElapsedColumn(),
)


def match_alias(alias: TrackAlias, s: Session) -> tuple[int, list[Track]]:
    track_filtersets = generate_match_filter_sets(
        (Track.title_norm, alias.title_norm),
        (Track.artist_norm, alias.artist_norm),
        (Track.album_norm, alias.album_norm),
        Track.album_artist_norm,
    )
    track_file_filtersets = generate_match_filter_sets(
        (TrackFile.title_norm, alias.title_norm),
        (TrackFile.artist_norm, alias.artist_norm),
        (TrackFile.album_norm, alias.album_norm),
        TrackFile.album_artist_norm,
    )
    # match every way possible, then get one that returned fewer tracks (but > 0)
    try_tracks = {}
    i = 0
    for track_wheres, track_file_wheres in zip(track_filtersets, track_file_filtersets):
        # match with Track first
        i += 1
        stmt = select(Track).where(*track_wheres)
        tracks = s.exec(stmt).all()
        if tracks:
            try_tracks[i] = list(tracks)

        # match with TrackFile next
        i += 1
        stmt = select(TrackFile).where(*track_file_wheres)
        tracks_files = s.exec(stmt).all()
        if tracks_files:
            # track files may point to the same track
            tracks = [tf.track for tf in tracks_files]
            tracks_uniq = []
            seen = set()
            for t in tracks:
                if t.id not in seen:
                    seen.add(t.id)
                    tracks_uniq.append(t)
            if tracks_uniq:
                try_tracks[i] = tracks_uniq

    sorted_items = sorted(try_tracks.items(), key=lambda item: len(item[1]))
    return sorted_items[0] if sorted_items else (0, [])


def match_aliases(reset: bool = False, dry_run: bool = False):
    cnt = Counter()
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

            i, tracks = match_alias(alias, s)
            if len(tracks) == 1:
                match += 1
                track: Track = tracks[0]
                alias.track = track
                if match % 100 == 0:
                    s.flush()
            elif not tracks:
                mismatch += 1
            else:
                multimatch += 1

            progress.update(
                task_id,
                advance=1,
                match=match,
                mismatch=mismatch,
                multimatch=multimatch,
            )

            cnt[(i, len(tracks))] += 1

        if dry_run:
            s.rollback()
            print("dry run, no changes were made")
        else:
            s.commit()
            print("changes committed")
        print()
        print("match results:")
        for (attempt, tracks_n), aliases_n in cnt.most_common():
            print(f"{attempt:>3} attempt {tracks_n:>3} tracks:", aliases_n)
