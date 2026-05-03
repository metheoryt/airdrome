from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlmodel import Session, func, select

from airdrome.cloud.apple.models import (
    AppleMediaServicesPlaylist,
    AppleMediaServicesTrack,
    ApplePlaylist,
    AppleTrack,
)
from airdrome.cloud.apple.unify import unify_apple_playlists, unify_apple_tracks
from airdrome.console import console


def do_unify(s: Session):
    xml_track_count = s.exec(
        select(func.count()).select_from(AppleTrack).where(AppleTrack.track_id.is_(None))
    ).one()
    ms_track_count = s.exec(select(func.count()).select_from(AppleMediaServicesTrack)).one()
    xml_pl_count = s.exec(
        select(func.count())
        .select_from(ApplePlaylist)
        .where(~ApplePlaylist.master, ~ApplePlaylist.music, ~ApplePlaylist.folder)
    ).one()
    ms_pl_count = s.exec(select(func.count()).select_from(AppleMediaServicesPlaylist)).one()

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

    console.print(
        f"  Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]  "
        f"[cyan]{files_bound} files bound[/cyan]\n"
        f"  Playlists: [magenta]{pl_created} new[/magenta]  [blue]{tr_linked} tracks linked[/blue]"
    )
