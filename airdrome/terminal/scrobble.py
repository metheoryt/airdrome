import typer
from sqlmodel import Session

from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.enums import Platform
from airdrome.models import TrackAlias, engine
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.match_aliases import match_aliases


scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")


@scrobble_app.command("match")
def scrobble_match(
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = typer.Option(False, "--dry-run", "-d"),
    threshold: float = typer.Option(0.4, "--threshold", "-t"),
):
    match_aliases(reset=reset, dry_run=dry_run, threshold=threshold)


SCROBBLE_PARSERS = {
    Platform.LISTENBRAINZ: ListenBrainzScrobbleParser,
    Platform.LASTFM: LastFMScrobbleParser,
    Platform.APPLE: AppleScrobbleParser,
    Platform.SPOTIFY: SpotifyScrobbleParser,
}


@scrobble_app.command("import")
def scrobble_import(
    platform: Platform,
    path: str = typer.Argument("path to the scrobbles file/directory"),
    recreate: bool = typer.Option(False, "--reset", "-r"),
):
    parser = SCROBBLE_PARSERS[platform](path)

    with Session(engine) as session:
        if recreate:
            TrackAlias.truncate_cascade(session)
            print("all previous track aliases/scrobbles are truncated")
        print("Importing", parser.platform, "scrobbles")
        aim, aig, ask, sim, sig = parser.import_aliases_scrobbles(session)
        print(parser.platform, "stats:")
        print("Aliases created/ignored/skipped:", f"{aim}/{aig}/{ask}")
        print("Scrobbles created/skipped:", f"{sim}/{sig}")


@scrobble_app.command("augment")
def scrobble_augment(dry_run: bool = typer.Option(False, "--dry-run", "-d")):
    with Session(engine) as session:
        augment_aliases(session, dry_run=dry_run)
