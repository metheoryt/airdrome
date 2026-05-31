import typer

from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.copy_plays import copy_plays
from airdrome.scrobbles.match_aliases import match_aliases

from .state import AppState


scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")


@scrobble_app.command("augment", help="run this after importing all scrobbles, to augment existing aliases")
def scrobble_augment(ctx: typer.Context, dry_run: bool = _DRY_RUN):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    augment_aliases(state.session)


@scrobble_app.command("match")
def scrobble_match(
    ctx: typer.Context,
    reset: bool = typer.Option(False, "--reset", "-r"),
    threshold: float = typer.Option(0.4, "--threshold", "-t"),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    match_aliases(state.session, reset=reset, threshold=threshold)


@scrobble_app.command("copy-plays", help="Copy scrobbles to TrackPlay rows for all matched aliases.")
def scrobble_copy_plays(
    ctx: typer.Context,
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    copy_plays(state.session, reset=reset)
