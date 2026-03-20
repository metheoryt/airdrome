from pathlib import Path

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, select

from airdrome.match import find_best_track
from airdrome.models import Track, TrackFile, engine


class FileIndexer:
    EXTENSIONS = {".mp3", ".m4a", ".flac"}

    def __init__(self, library_path: Path, match_threshold: float = 0.4):
        self.library_path = library_path
        self.match_threshold = match_threshold
        self.progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("✅ {task.fields[file_created]} new files "),
            TextColumn("✅ {task.fields[track_created]} new tracks "),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )

    def index_track(self, abs_path: Path, s: Session, match_threshold: float = 0.4):
        # check whether it's already in the database
        created = track_created = False
        tf = s.exec(select(TrackFile).where(TrackFile.path == abs_path)).one_or_none()
        if not tf:
            tf = TrackFile(path=abs_path)
            s.add(tf)
            created = True

        # get tags and metadata
        tf.enrich()

        if not tf.track:
            # try to match it with an existing track
            track = find_best_track(
                s, tf.title_norm, tf.artist_norm, tf.album_norm, threshold=match_threshold
            )
            if not track:
                # create a new track
                tf.track = Track(
                    title=tf.title, artist=tf.artist, album_artist=tf.album_artist, album=tf.album
                )
                s.add(tf)
                track_created = True

        return tf, created, track_created

    def index_library(self):
        with Session(engine) as s, self.progress:
            n_created = n_tracks_created = 0
            task_id = self.progress.add_task(
                f"Indexing {self.library_path}",
                total=None,
                file_created=n_created,
                track_created=n_tracks_created,
            )
            for abs_path in self.library_path.rglob("*"):
                if not abs_path.is_file() or abs_path.suffix not in self.EXTENSIONS:
                    continue

                tf, created, track_created = self.index_track(abs_path, s)
                if created:
                    n_created += 1
                if track_created:
                    n_tracks_created += 1
                self.progress.update(
                    task_id,
                    advance=1,
                    file_created=n_created,
                    track_created=n_tracks_created,
                )
                s.flush()
            s.commit()
