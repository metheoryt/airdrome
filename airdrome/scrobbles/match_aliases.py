from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, func, select, update

from airdrome.console import console
from airdrome.match import find_best_track
from airdrome.models import TrackAlias, engine


def do_match_aliases(
    s: Session,
    reset: bool = False,
    dry_run: bool = False,
    threshold: float = 0.4,
) -> tuple[int, int]:
    """
    Core alias-matching logic. Returns (matched, unmatched).

    Testable directly — no session creation, no progress output.
    """
    if reset:
        s.exec(update(TrackAlias).values(track_id=None))
        s.flush()

    aliases = s.exec(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()
    matched = unmatched = 0

    sp = s.begin_nested() if dry_run else None

    for alias in aliases:
        track = find_best_track(s, alias.title_norm, alias.artist_norm, alias.album_norm, threshold=threshold)
        if track:
            matched += 1
            alias.track = track
            if matched % 100 == 0:
                s.flush()
        else:
            unmatched += 1

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

        total = s.exec(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_(None))).one()
        label = f"Matching {total} aliases{' [dry run]' if dry_run else ''}"

        with progress:
            task = progress.add_task(label, total=total, match=0, mismatch=0)

            def _on_result(matched: int, unmatched: int):
                progress.update(task, advance=1, match=matched, mismatch=unmatched)

            aliases = s.exec(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()
            matched = unmatched = 0
            for alias in aliases:
                track = find_best_track(
                    s, alias.title_norm, alias.artist_norm, alias.album_norm, threshold=threshold
                )
                if track:
                    matched += 1
                    alias.track = track
                    if matched % 100 == 0:
                        s.flush()
                else:
                    unmatched += 1
                _on_result(matched, unmatched)

            if dry_run:
                s.rollback()
                console.print("[dim]dry run — no changes saved[/dim]")
            else:
                s.commit()
                console.print(f"[green]matched: {matched}  unmatched: {unmatched}[/green]")
