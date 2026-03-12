import typer
from rich.console import Console

from airdrome.navidrome import sync_apple_playlists_to_navi, sync_tracks_plays_to_navi


console = Console()
navidrome_app = typer.Typer(help="Airdrome Navidrome CLI")


@navidrome_app.command("sync-playlists")
def navidrome_playlists(username: str):
    console.print("[bold green]Syncing airdrome playlists to Navidrome[/bold green]")
    sync_apple_playlists_to_navi(username)
    console.print("[bold green]Sync completed[/bold green]")


@navidrome_app.command("sync-tracks")
def navidrome_tracks(username: str, reset: bool = typer.Option(False, "--reset", "-r")):
    console.print("[bold green]Syncing airdrome tracks and scrobbles to Navidrome[/bold green]")
    sync_tracks_plays_to_navi(username, reset)
    console.print("[bold green]Sync completed[/bold green]")
