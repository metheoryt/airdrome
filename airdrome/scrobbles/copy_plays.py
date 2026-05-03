from collections.abc import Callable

from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, delete, func, select

from airdrome.console import console
from airdrome.models import TrackAlias, TrackPlay


def do_copy_plays(
    s: Session,
    reset: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """
    Creates TrackPlay rows from scrobbles on already-matched aliases. Returns play count.

    on_progress(aliases_processed) is called after each alias is processed.
    Testable directly — no session creation, no progress output.
    """
    if reset:
        s.exec(delete(TrackPlay).where(TrackPlay.source_scrobble_id.is_not(None)))
        s.flush()

    aliases = s.exec(select(TrackAlias).where(TrackAlias.track_id.is_not(None))).all()
    total = 0

    for i, alias in enumerate(aliases):
        for scrobble in alias.scrobbles:
            existing = s.exec(
                select(TrackPlay).where(TrackPlay.source_scrobble_id == scrobble.id)
            ).one_or_none()
            if existing:
                existing.track_id = alias.track_id
            else:
                s.add(
                    TrackPlay(
                        track_id=alias.track_id,
                        played_at=scrobble.date,
                        platform=scrobble.platform,
                        source_scrobble_id=scrobble.id,
                    )
                )
            total += 1

        if (i + 1) % 100 == 0:
            s.flush()

        if on_progress:
            on_progress(i + 1)

    return total


def copy_plays(s: Session, reset: bool = False):
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} aliases  "),
        TimeElapsedColumn(),
    )

    if reset:
        console.print("[yellow]dropping all scrobble-derived plays[/yellow]")

    total = s.exec(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_not(None))).one()

    with progress:
        task = progress.add_task(f"Copying plays from {total} matched aliases", total=total)

        def _on_progress(aliases_done: int):
            progress.update(task, completed=aliases_done)

        plays = do_copy_plays(s, reset=reset, on_progress=_on_progress)

    console.print(f"[green]plays copied: {plays}[/green]")
