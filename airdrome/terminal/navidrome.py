import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.navidrome import sync_apple_playlists_to_navi, sync_tracks_plays_to_navi


navidrome_app = typer.Typer(help="Navidrome sync")
push_app = typer.Typer(help="Push data from Airdrome to Navidrome")
pull_app = typer.Typer(help="Pull data from Navidrome into Airdrome")

navidrome_app.add_typer(push_app, name="push")
navidrome_app.add_typer(pull_app, name="pull")


def _require_user() -> str:
    if not settings.navidrome_user:
        console.print("NAVIDROME_USER is not configured in .env", style="bold red")
        raise typer.Exit(code=1)
    return settings.navidrome_user


@push_app.command("playlists")
def push_playlists():
    username = _require_user()
    console.print("[bold green]Pushing playlists to Navidrome[/bold green]")
    sync_apple_playlists_to_navi(username)
    console.print("[bold green]Done[/bold green]")


@push_app.command("tracks")
def push_tracks(reset: bool = typer.Option(False, "--reset", "-r")):
    username = _require_user()
    console.print("[bold green]Pushing tracks and scrobbles to Navidrome[/bold green]")
    sync_tracks_plays_to_navi(username, reset)
    console.print("[bold green]Done[/bold green]")


@pull_app.command("plays")
def pull_plays(reset: bool = typer.Option(False, "--reset", "-r")):
    _require_user()
    console.print("[yellow]navidrome pull plays: not yet implemented[/yellow]")
    raise typer.Exit(code=1)


@pull_app.command("ratings")
def pull_ratings(reset: bool = typer.Option(False, "--reset", "-r")):
    _require_user()
    console.print("[yellow]navidrome pull ratings: not yet implemented[/yellow]")
    raise typer.Exit(code=1)
