import socket

import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.navidrome import checkpoint_wal, sync_tracks_plays_to_navi
from airdrome.navidrome.adapter import NavidromeAdapter
from airdrome.playlists import sync as sync_playlists

from .options import YES
from .state import AppState


navidrome_app = typer.Typer(help="Navidrome sync")


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


@navidrome_app.command("playlists")
def sync_playlists_cmd(ctx: typer.Context, yes: bool = YES):
    """3-way merge every playlist between Airdrome and Navidrome."""
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    console.print("[bold]Syncing playlists with Navidrome[/bold]")
    state: AppState = ctx.obj
    with NavidromeAdapter(state.session, username) as adapter:
        sync_playlists(state.session, adapter)


@navidrome_app.command("push")
def push_tracks(ctx: typer.Context, yes: bool = YES):
    """Push play counts and ratings for NAVIDROME_USER into Navidrome."""
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    console.print("[bold]Pushing tracks and scrobbles to Navidrome[/bold]")
    state: AppState = ctx.obj
    sync_tracks_plays_to_navi(state.session, username)
