from pathlib import Path

import typer
from sqlmodel import Session

from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.console import console
from airdrome.enums import Platform
from airdrome.models import TrackAlias, engine
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.match_aliases import match_aliases


scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")


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
    platform: Platform,
    path: Path = typer.Argument(help="path to the scrobbles zip file/directory"),
    recreate: bool = typer.Option(False, "--reset", "-r"),
):
    parser = SCROBBLE_PARSERS[platform](path)

    with Session(engine) as session:
        if recreate:
            TrackAlias.truncate_cascade(session)
            console.print("[yellow]all previous track aliases/scrobbles truncated[/yellow]")
        console.print(f"Importing [bold]{parser.platform}[/bold] scrobbles")
        stats = parser.import_aliases_scrobbles(session)
        console.print(
            f"  aliases:   [cyan]{stats.aliases_created}[/cyan] created "
            f"/ {stats.aliases_ignored} ignored "
            f"/ {stats.aliases_skipped} skipped"
        )
        console.print(
            f"  scrobbles: [cyan]{stats.scrobbles_created}[/cyan] created / {stats.scrobbles_ignored} ignored"
        )


@scrobble_app.command("augment", help="run this after importing all scrobbles, to augment existing aliases")
def scrobble_augment(dry_run: bool = typer.Option(False, "--dry-run", "-d")):
    with Session(engine) as session:
        augment_aliases(session, dry_run=dry_run)


@scrobble_app.command("match")
def scrobble_match(
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = typer.Option(False, "--dry-run", "-d"),
    threshold: float = typer.Option(0.4, "--threshold", "-t"),
):
    match_aliases(reset=reset, dry_run=dry_run, threshold=threshold)
