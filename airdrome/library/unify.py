from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from airdrome.cloud.apple.models import AppleMSPlaylist, AppleMSTrack, ApplePlaylist, AppleTrack
from airdrome.cloud.apple.unify import unify_apple_playlists, unify_apple_tracks
from airdrome.console import console
from airdrome.models import Playlist, Track, TrackFile


def _unify_orphan_files(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    created = updated = 0
    stmt = select(TrackFile).where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
    for tf in s.scalars(stmt):
        year = None
        if tf.date:
            try:
                year = int(tf.date[:4])
            except ValueError, IndexError:
                pass
        track_defaults = {
            "duration": round(tf.duration) if tf.duration else None,
            "year": year,
        }
        track, track_created = Track.get_or_create(
            s,
            title=tf.title,
            artist=tf.artist,
            album=tf.album,
            album_artist=tf.album_artist,
            defaults=track_defaults,
        )
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        tf.track = track
        s.flush()
        progress.update(task, advance=1, created=created, updated=updated)

    return created, updated


def do_unify(s: Session, reset_playlists: bool = False):
    if reset_playlists:
        s.execute(delete(Playlist))
        s.flush()
        console.print("[yellow]Canonical playlists reset[/yellow]")

    xml_track_count = s.scalars(
        select(func.count()).select_from(AppleTrack).where(AppleTrack.track_id.is_(None))
    ).one()
    ms_track_count = s.scalars(select(func.count()).select_from(AppleMSTrack)).one()
    xml_pl_count = s.scalars(
        select(func.count())
        .select_from(ApplePlaylist)
        .where(~ApplePlaylist.master, ~ApplePlaylist.music, ~ApplePlaylist.folder)
    ).one()
    ms_pl_count = s.scalars(select(func.count()).select_from(AppleMSPlaylist)).one()
    orphan_count = s.scalars(
        select(func.count())
        .select_from(TrackFile)
        .where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
    ).one()

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[green]{task.fields[created]} new[/green]  "
            "[yellow]{task.fields[updated]} updated[/yellow]  "
            "[cyan]{task.fields[files_bound]} files bound[/cyan]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Tracks",
            total=xml_track_count + ms_track_count,
            created=0,
            updated=0,
            files_bound=0,
        )
        created, updated, files_bound = unify_apple_tracks(s, progress, task)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[magenta]{task.fields[pl_created]} playlists[/magenta]  "
            "[blue]{task.fields[tr_linked]} linked[/blue]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Playlists",
            total=xml_pl_count + ms_pl_count,
            pl_created=0,
            tr_linked=0,
        )
        pl_created, tr_linked = unify_apple_playlists(s, progress, task)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[green]{task.fields[created]} new[/green]  [yellow]{task.fields[updated]} updated[/yellow]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Orphan files", total=orphan_count, created=0, updated=0)
        orphan_created, orphan_updated = _unify_orphan_files(s, progress, task)

    console.print(
        f"  Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]  "
        f"[cyan]{files_bound} files bound[/cyan]\n"
        f"  Playlists: [magenta]{pl_created} new[/magenta]  [blue]{tr_linked} tracks linked[/blue]\n"
        f"  Orphan files: [green]{orphan_created} new[/green]  [yellow]{orphan_updated} updated[/yellow]"
    )
