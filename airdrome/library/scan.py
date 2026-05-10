from pathlib import Path
from typing import Callable

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.models import TrackFile


class MusicScanner:
    EXTENSIONS = {".mp3", ".m4a", ".flac"}

    def __init__(self, target_path: Path, match_threshold: float = 0.4):
        self.target_path = target_path

    def scan_file(self, abs_path: Path, s: Session) -> tuple[TrackFile, bool]:
        created = False
        tf = s.scalars(select(TrackFile).where(TrackFile.source_path == abs_path)).one_or_none()
        if not tf:
            tf = TrackFile(source_path=abs_path)
            s.add(tf)
            created = True
        tf.enrich()
        return tf, created

    def scan_all(self, s: Session, _on_item: Callable[[int], None] | None = None) -> int:
        """
        Core scan logic. Returns number of new TrackFile records created.

        Testable directly — no session creation, no progress output.
        """
        n_created = 0
        for abs_path in self.target_path.rglob("*"):
            if not abs_path.is_file() or abs_path.suffix not in self.EXTENSIONS:
                continue

            _, created = self.scan_file(abs_path, s)
            if created:
                n_created += 1
            s.flush()
            if _on_item:
                _on_item(n_created)

        return n_created

    def run(self, s: Session):
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("✅ {task.fields[file_created]} new files"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        with progress:
            task_id = progress.add_task(
                f"Scanning {self.target_path}",
                total=None,
                file_created=0,
            )

            def _on_item(n_created: int):
                progress.update(task_id, advance=1, file_created=n_created)

            self.scan_all(s, _on_item=_on_item)
