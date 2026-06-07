from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.console import done, make_progress
from airdrome.models import TrackAlias, TrackPlay


def do_copy_plays(
    s: Session,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """
    Creates TrackPlay rows from scrobbles on already-matched aliases. Returns play count.

    on_progress(aliases_processed) is called after each alias is processed.
    Testable directly — no session creation, no progress output.
    """
    aliases = s.scalars(select(TrackAlias).where(TrackAlias.track_id.is_not(None))).all()
    total = 0

    for i, alias in enumerate(aliases):
        for scrobble in alias.scrobbles:
            existing = s.scalars(
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


def copy_plays(s: Session):
    """Materialize TrackPlay history from matched aliases, with a progress bar and summary."""
    total = s.scalars(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_not(None))).one()

    with make_progress() as progress:
        task = progress.add_task("Copying plays from matched aliases", total=total)

        def _on_progress(aliases_done: int):
            progress.update(task, completed=aliases_done)

        plays = do_copy_plays(s, on_progress=_on_progress)

    done(f"{plays} plays copied")
