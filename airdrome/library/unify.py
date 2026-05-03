from sqlmodel import Session

from airdrome.cloud.apple.unify import unify_apple_playlists, unify_apple_tracks
from airdrome.console import console


def do_unify(s: Session):
    # Phase 1: tracks (all platforms)
    console.print("[bold]Unifying tracks...[/bold]")
    created, updated = unify_apple_tracks(s)
    console.print(f"  Apple: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]")

    # Phase 2: playlists — depends on tracks being linked first
    console.print("[bold]Unifying playlists...[/bold]")
    pl_created, tr_linked = unify_apple_playlists(s)
    console.print(f"  Apple: [green]{pl_created} playlists[/green]  [cyan]{tr_linked} tracks linked[/cyan]")
