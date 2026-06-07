from rich.progress import TextColumn
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from airdrome.console import done, make_progress
from airdrome.models import TrackAlias


def maybe_complete_alias(alias: TrackAlias, s: Session):
    # implied that the title is not empty

    if alias.album_norm and alias.artist_norm:
        # no need to complete the data
        return []

    wheres = [TrackAlias.title_norm == alias.title_norm]

    or_wheres = []
    if not alias.artist_norm:
        or_wheres.append(TrackAlias.artist_norm != "")
    if not alias.album_norm:
        or_wheres.append(TrackAlias.album_norm != "")

    if or_wheres:
        wheres.append(or_(*or_wheres))

    matched_aliases = s.scalars(select(TrackAlias).where(*wheres)).all()
    matched_artist = matched_album = None
    if not len(matched_aliases):
        # no matches
        return []

    elif len(matched_aliases) > 1:
        # multiple matches: combine the data
        artist_set = {ma.artist_norm for ma in matched_aliases if ma.artist_norm}
        album_set = {ma.album_norm for ma in matched_aliases if ma.album_norm}

        if len(artist_set) == 1:
            matched_artist = next(iter(artist_set))

        if len(album_set) == 1:
            matched_album = next(iter(album_set))
    else:
        # exactly one match
        match: TrackAlias = matched_aliases[0]
        matched_artist = match.artist_norm
        matched_album = match.album_norm

    changed = []
    if not alias.album_norm and matched_album:
        alias.album_norm = matched_album
        changed.append("album")
    if not alias.artist_norm and matched_artist:
        alias.artist_norm = matched_artist
        changed.append("artist")

    return changed


def augment_aliases(s: Session):
    """Backfill blank artist/album on aliases from sibling aliases, with a progress bar and summary."""
    aliases = s.scalars(
        select(TrackAlias).where(or_(TrackAlias.album_norm == "", TrackAlias.artist_norm == ""))
    ).all()
    progress = make_progress(
        TextColumn("[green]{task.fields[full]} full[/green]"),
        TextColumn("[cyan]{task.fields[partial]} partial[/cyan]"),
        TextColumn("[dim]{task.fields[no]} unchanged[/dim]"),
    )
    full = partial = no = 0
    with progress:
        task_id = progress.add_task("Augmenting aliases", total=len(aliases), full=0, partial=0, no=0)
        for alias in aliases:
            completed = maybe_complete_alias(alias, s)
            if len(completed) == 2:
                full += 1
            elif len(completed) == 1:
                partial += 1
            else:
                no += 1
            progress.update(task_id, advance=1, full=full, partial=partial, no=no)

    done(f"augmented {full} fully, {partial} partially ({no} unchanged)")
