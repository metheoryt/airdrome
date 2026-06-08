"""The `navi` group: sync into Navidrome.

A group per destination (not a `--destination` flag) because backends diverge in connection
config, "must be stopped" semantics, and capabilities — a future `plex` group would be a sibling
here. Today `push` writes Navidrome's SQLite DB directly, so it requires Navidrome stopped.
"""

import socket

import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.navidrome import checkpoint_wal, sync_tracks_plays_to_navi

from .options import YES
from .state import AppState


navi_app = typer.Typer(help="Sync into Navidrome")


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


@navi_app.command("push")
def push(ctx: typer.Context, yes: bool = YES):
    """Push play counts and ratings for NAVIDROME_USER into Navidrome.

    Writes Navidrome's SQLite DB directly, so it requires Navidrome stopped. Playlists are
    no longer pushed here — reconcile them with `airdrome sync navidrome` (or `sync all`).
    """
    username = _require_user()
    _guard_navidrome_stopped(yes)
    checkpoint_wal()
    state: AppState = ctx.obj

    console.print("[bold]Pushing play counts and ratings to Navidrome[/bold]")
    sync_tracks_plays_to_navi(state.session, username)
