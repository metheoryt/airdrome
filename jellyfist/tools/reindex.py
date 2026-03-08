from pathlib import Path

from sqlmodel import Session, select
from jellyfist.models import TrackFile, engine, Track
from jellyfist.match import generate_match_filter_sets
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn, MofNCompleteColumn


EXTENSIONS = {".mp3", ".m4a", ".flac"}


progress = Progress(
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("✅ {task.fields[file_created]} new files "),
    TextColumn("✅ {task.fields[track_created]} new tracks "),
    TextColumn("✅ {task.fields[match]} files matched "),
    TextColumn("❌ {task.fields[mismatch]} files mismatched "),
    MofNCompleteColumn(),
    TimeElapsedColumn(),
)


def match_file_with_track(tf: TrackFile, s: Session) -> list[Track]:
    if not tf.title_norm:
        # don't match the track if it doesn't have a title
        return []

    filtersets = generate_match_filter_sets(
        title=(Track.title_norm, tf.title_norm),
        artist=(Track.artist_norm, tf.artist_norm),
        album=(Track.album_norm, tf.album_norm),
        album_artist_col=Track.album_artist_norm,
    )

    matched_tracks = []
    for filterset in filtersets:
        matched_tracks = s.exec(select(Track).where(*filterset)).all()
        if matched_tracks:
            break
    return list(matched_tracks)


def index_track(base_path: Path, rel_path: Path, s: Session):
    # check whether it's already in the database
    created = track_created = False
    match = None
    tf = s.exec(select(TrackFile).where(TrackFile.path == rel_path)).one_or_none()
    if not tf:
        tf = TrackFile(path=rel_path)
        s.add(tf)
        created = True

    TrackFile.enrich(tf, base_path=base_path)

    if tf.track:
        # no match required
        return tf, created, track_created, match

    # try to match it with an existing track
    tracks = match_file_with_track(tf, s)
    if not tracks:
        # create a new track
        tf.track = Track(title=tf.title, artist=tf.artist, album_artist=tf.album_artist, album=tf.album)
        s.add(tf)
        track_created = True
    elif len(tracks) == 1:
        tf.track = tracks[0]
        match = True
    else:
        # multiple matches: don't link any track, will do that manually
        match = False

    return tf, created, track_created, match


def index_library(library_path: Path):
    with Session(engine) as s, progress:
        n_created = n_tracks_created = n_matched = n_mismatched = 0
        task_id = progress.add_task(
            f"Indexing {library_path}",
            total=None,
            file_created=n_created,
            track_created=n_tracks_created,
            match=n_matched,
            mismatch=n_mismatched,
        )
        for path in library_path.rglob("*"):
            if not path.is_file() or path.suffix not in EXTENSIONS:
                continue

            relative_path = path.relative_to(library_path)
            tf, created, track_created, match = index_track(library_path, relative_path, s)
            if created:
                n_created += 1
            if track_created:
                n_tracks_created += 1
            if match:
                n_matched += 1
            elif match is False:
                n_mismatched += 1
            progress.update(
                task_id,
                advance=1,
                file_created=n_created,
                track_created=n_tracks_created,
                match=n_matched,
                mismatch=n_mismatched,
            )
            s.flush()
        s.commit()
