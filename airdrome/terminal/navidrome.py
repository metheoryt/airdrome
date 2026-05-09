import socket

import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.navidrome import checkpoint_wal, sync_tracks_plays_to_navi
from airdrome.navidrome.adapter import NavidromeAdapter
from airdrome.playlists import sync as sync_playlists

from .state import AppState


navidrome_app = typer.Typer(help="Navidrome sync")
sync_app = typer.Typer(help="Bidirectional sync between Airdrome and Navidrome")
push_app = typer.Typer(help="Push data from Airdrome to Navidrome (one-way)")

navidrome_app.add_typer(sync_app, name="sync")
navidrome_app.add_typer(push_app, name="push")


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


@sync_app.command("playlists")
def sync_playlists_cmd(ctx: typer.Context, yes: bool = _YES_OPT):
    """3-way merge every playlist between Airdrome and Navidrome."""
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    console.print("[bold green]Syncing playlists with Navidrome[/bold green]")
    state: AppState = ctx.obj
    with NavidromeAdapter(state.session, username) as adapter:
        sync_playlists(state.session, adapter)
    console.print("[bold green]Done[/bold green]")


@push_app.command("tracks")
def push_tracks(
    ctx: typer.Context,
    reset: bool = typer.Option(False, "--reset", "-r"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the Navidrome-stopped confirmation"),
):
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    console.print("[bold green]Pushing tracks and scrobbles to Navidrome[/bold green]")
    state: AppState = ctx.obj
    sync_tracks_plays_to_navi(state.session, username, reset)
    console.print("[bold green]Done[/bold green]")
