import socket

import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.navidrome import checkpoint_wal, sync_apple_playlists_to_navi, sync_tracks_plays_to_navi


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


def _guard_navidrome_stopped(yes: bool):
    """Abort if Navidrome is listening on localhost; prompt when --yes is not passed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        if sock.connect_ex(("localhost", settings.navidrome_port)) == 0:
            console.print(
                f"[bold red]Navidrome is running on port {settings.navidrome_port}. "
                "Stop it before syncing to avoid database corruption.[/bold red]"
            )
            raise typer.Exit(code=1)

    if not yes:
        typer.confirm(
            "This command writes directly to Navidrome's SQLite database.\nConfirm Navidrome is stopped",
            abort=True,
        )


_YES_OPT = typer.Option(False, "--yes", "-y", help="Skip the Navidrome-stopped confirmation")


@push_app.command("playlists")
def push_playlists(yes: bool = _YES_OPT):
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    console.print("[bold green]Pushing playlists to Navidrome[/bold green]")
    sync_apple_playlists_to_navi(username)
    console.print("[bold green]Done[/bold green]")


@push_app.command("tracks")
def push_tracks(
    reset: bool = typer.Option(False, "--reset", "-r"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the Navidrome-stopped confirmation"),
):
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
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
