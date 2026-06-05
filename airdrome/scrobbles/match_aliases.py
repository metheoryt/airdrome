from collections.abc import Callable

from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.match import find_best_track
from airdrome.models import TrackAlias


def do_match_aliases(
    s: Session,
    threshold: float = 0.4,
    on_progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """
    Sets TrackAlias.track_id for unmatched aliases. Returns (matched, unmatched).

    on_progress(matched, unmatched) is called after each alias is processed.
    Testable directly — no session creation, no progress output.
    """
    aliases = s.scalars(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()
    matched = unmatched = 0

    for alias in aliases:
        track = find_best_track(
            s, alias.title_norm, alias.artist_norm, alias.album_norm, threshold=threshold, log=log
        )
        if track:
            matched += 1
            alias.track = track
            if matched % 100 == 0:
                s.flush()
        else:
            unmatched += 1

        if on_progress:
            on_progress(matched, unmatched)

    s.flush()
    return matched, unmatched


def match_aliases(s: Session, threshold: float = 0.4):
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("✅ {task.fields[match]}  "),
        TextColumn("❌ {task.fields[mismatch]}  "),
        TimeElapsedColumn(),
    )

    total = s.scalars(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_(None))).one()

    with progress:
        task = progress.add_task(f"Matching {total} aliases", total=total, match=0, mismatch=0)

        def _on_progress(matched: int, unmatched: int):
            progress.update(task, advance=1, match=matched, mismatch=unmatched)

        matched, unmatched = do_match_aliases(
            s,
            threshold=threshold,
            on_progress=_on_progress,
            log=progress.console.print,
        )

    console.print(f"[green]matched: {matched}  unmatched: {unmatched}[/green]")
