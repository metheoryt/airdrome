from collections.abc import Callable

from rich.progress import TextColumn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.console import done, is_verbose, make_progress
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
    """Match unmatched aliases to canonical tracks, with a progress bar and summary."""
    progress = make_progress(
        TextColumn("[green]✓ {task.fields[match]}[/green]"),
        TextColumn("[red]✗ {task.fields[mismatch]}[/red]"),
    )

    total = s.scalars(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_(None))).one()

    with progress:
        task = progress.add_task("Matching aliases", total=total, match=0, mismatch=0)

        def _on_progress(matched: int, unmatched: int):
            progress.update(task, advance=1, match=matched, mismatch=unmatched)

        matched, unmatched = do_match_aliases(
            s,
            threshold=threshold,
            on_progress=_on_progress,
            # Per-alias match reasoning is verbose-only; otherwise it floods the bar.
            log=progress.console.print if is_verbose() else None,
        )

    done(f"matched {matched}, unmatched {unmatched}")
