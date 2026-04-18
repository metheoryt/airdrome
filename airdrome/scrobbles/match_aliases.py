from collections.abc import Callable

from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, delete, func, select, update

from airdrome.console import console
from airdrome.match import find_best_track
from airdrome.models import TrackAlias, TrackPlay, engine


def do_match_aliases(
    s: Session,
    reset: bool = False,
    dry_run: bool = False,
    threshold: float = 0.4,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """
    Core alias-matching logic. Returns (matched, unmatched).

    on_progress(matched, unmatched) is called after each alias is processed.
    Testable directly — no session creation, no progress output.
    """
    if reset:
        s.exec(update(TrackAlias).values(track_id=None))
        s.exec(delete(TrackPlay).where(TrackPlay.source_scrobble_id.is_not(None)))
        s.flush()

    aliases = s.exec(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()
    matched = unmatched = 0

    sp = s.begin_nested() if dry_run else None

    for alias in aliases:
        track = find_best_track(s, alias.title_norm, alias.artist_norm, alias.album_norm, threshold=threshold)
        if track:
            matched += 1
            alias.track = track
            for scrobble in alias.scrobbles:
                existing = s.exec(
                    select(TrackPlay).where(TrackPlay.source_scrobble_id == scrobble.id)
                ).one_or_none()
                if existing:
                    existing.track_id = track.id
                else:
                    s.add(
                        TrackPlay(
                            track_id=track.id,
                            played_at=scrobble.date,
                            platform=scrobble.platform,
                            source_scrobble_id=scrobble.id,
                        )
                    )
            if matched % 100 == 0:
                s.flush()
        else:
            unmatched += 1

        if on_progress:
            on_progress(matched, unmatched)

    if dry_run:
        sp.rollback()
    else:
        s.commit()

    return matched, unmatched


def match_aliases(reset: bool = False, dry_run: bool = False, threshold: float = 0.4):
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("✅ {task.fields[match]}  "),
        TextColumn("❌ {task.fields[mismatch]}  "),
        TimeElapsedColumn(),
    )
    with Session(engine) as s:
        if reset:
            console.print("[yellow]dropping all alias-track links[/yellow]")

        count_stmt = select(func.count(TrackAlias.id))
        if not reset:
            count_stmt = count_stmt.where(TrackAlias.track_id.is_(None))
        total = s.exec(count_stmt).one()

        label = f"Matching {total} aliases{' [dry run]' if dry_run else ''}"

        with progress:
            task = progress.add_task(label, total=total, match=0, mismatch=0)

            def _on_progress(matched: int, unmatched: int):
                progress.update(task, advance=1, match=matched, mismatch=unmatched)

            matched, unmatched = do_match_aliases(
                s,
                reset=reset,
                dry_run=dry_run,
                threshold=threshold,
                on_progress=_on_progress,
            )

        if dry_run:
            console.print("[dim]dry run — no changes saved[/dim]")
        else:
            console.print(f"[green]matched: {matched}  unmatched: {unmatched}[/green]")
