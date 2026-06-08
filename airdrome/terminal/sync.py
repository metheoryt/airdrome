"""The `sync` group: reconcile playlists across remotes.

One subcommand per playlist-providing remote, plus `sync all`. Sources (`apple_xml`,
`apple_ms`) are read-only; `navidrome` is a read-write backend, so any scope that
includes it requires Navidrome stopped (it writes the SQLite DB directly) and uses
NAVIDROME_USER. `sync all` runs sources first, then backends, so a source delete reaches
canonical before the backend push instead of being re-added. Bare `sync` prints help.
"""

import contextlib

import typer

from airdrome.console import console
from airdrome.enums import Source
from airdrome.navidrome import checkpoint_wal
from airdrome.navidrome.adapter import NavidromeAdapter
from airdrome.playlists import reconcile
from airdrome.playlists.source_remote import SourcePlaylistRemote

from .navi import _guard_navidrome_stopped, _require_user
from .options import DRY_RUN, REVIEW, YES
from .state import AppState


sync_app = typer.Typer(help="Reconcile playlists across remotes (cloud sources + backends).")

# `sync all` order: read-only sources first, then read-write backends.
ALL_REMOTES = (Source.APPLE_XML, Source.APPLE_MS, Source.NAVIDROME)


def _build_adapter(remote: Source, state: AppState):
    if remote is Source.NAVIDROME:
        return NavidromeAdapter(state.session, _require_user())
    return SourcePlaylistRemote(state.session, remote)


def _run(ctx: typer.Context, remotes: tuple[Source, ...], *, review: bool, dry_run: bool, yes: bool):
    state: AppState = ctx.obj
    state.dry_run = dry_run

    if Source.NAVIDROME in remotes:
        _require_user()
        _guard_navidrome_stopped(yes)  # writes NV's SQLite DB — refuse while it's running
        checkpoint_wal()

    with contextlib.ExitStack() as stack:
        adapters = [stack.enter_context(_build_adapter(r, state)) for r in remotes]
        reconcile(state.session, adapters, review=review)


def _command(remotes: tuple[Source, ...]):
    """Build a subcommand bound to a fixed set of remotes."""

    def cmd(ctx: typer.Context, review: bool = REVIEW, dry_run: bool = DRY_RUN, yes: bool = YES):
        _run(ctx, remotes, review=review, dry_run=dry_run, yes=yes)

    return cmd


sync_app.command("all", help="Reconcile every remote (sources first, then backends).")(_command(ALL_REMOTES))
sync_app.command("apple_xml", help="Reconcile the Apple iTunes XML source (read-only).")(
    _command((Source.APPLE_XML,))
)
sync_app.command("apple_ms", help="Reconcile the Apple Media Services source (read-only).")(
    _command((Source.APPLE_MS,))
)
sync_app.command("navidrome", help="Reconcile the Navidrome backend (Navidrome must be stopped).")(
    _command((Source.NAVIDROME,))
)


@sync_app.callback(invoke_without_command=True)
def _sync_callback(ctx: typer.Context):
    """Show help when `sync` is run without a remote."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
