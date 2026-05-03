from pathlib import Path

import typer

from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.console import console
from airdrome.enums import Platform
from airdrome.models import TrackAlias
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.copy_plays import copy_plays
from airdrome.scrobbles.match_aliases import match_aliases

from .state import AppState


scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")

SCROBBLE_PARSERS = {
    Platform.LISTENBRAINZ: ListenBrainzScrobbleParser,
    Platform.LASTFM: LastFMScrobbleParser,
    Platform.APPLE: AppleScrobbleParser,
    Platform.SPOTIFY: SpotifyScrobbleParser,
}


@scrobble_app.command(
    "import",
    help=(
        "Import listens from the specified platform. "
        "ListenBrainz is recommended. "
        "You can setup LastFM/Spotify listen data import to it, and then have a data export."
    ),
)
def scrobble_import(
    ctx: typer.Context,
    platform: Platform,
    path: Path = typer.Argument(help="path to the scrobbles zip file/directory"),
    recreate: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    parser = SCROBBLE_PARSERS[platform](path)
    if recreate:
        TrackAlias.truncate_cascade(state.session)
        console.print("[yellow]all previous track aliases/scrobbles truncated[/yellow]")
    console.print(f"Importing [bold]{parser.platform}[/bold] scrobbles")
    stats = parser.import_aliases_scrobbles(state.session)
    console.print(
        f"  aliases:   [cyan]{stats.aliases_created}[/cyan] created "
        f"/ {stats.aliases_ignored} ignored "
        f"/ {stats.aliases_skipped} skipped"
    )
    console.print(
        f"  scrobbles: [cyan]{stats.scrobbles_created}[/cyan] created / {stats.scrobbles_ignored} ignored"
    )


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
