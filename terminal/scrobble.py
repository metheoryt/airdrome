import typer
from sqlmodel import Session

from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.conf import settings
from airdrome.enums import Platform
from airdrome.models import TrackAlias, engine
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.match_aliases import match_aliases


scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")


@scrobble_app.command("match")
def match_cli(
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = typer.Option(False, "--dry-run", "-d"),
    threshold: float = typer.Option(0.4, "--threshold", "-t"),
):
    match_aliases(reset=reset, dry_run=dry_run, threshold=threshold)


SCROBBLE_PARSERS = {
    Platform.LISTENBRAINZ: ListenBrainzScrobbleParser(settings.listenbrainz_listens_dir_path),
    Platform.LASTFM: LastFMScrobbleParser(settings.lastfm_scrobbles_filepath),
    Platform.APPLE: AppleScrobbleParser(settings.apple_music_play_activity_filepath),
    Platform.SPOTIFY: SpotifyScrobbleParser(settings.spotify_streaming_history_dirpath),
}


@scrobble_app.command("import")
def scrobble_import(platform: Platform | None = None, recreate: bool = typer.Option(False, "--reset", "-r")):
    if platform:
        parsers = [SCROBBLE_PARSERS[platform]]
    else:
        print("Importing all scrobbles")
        parsers = SCROBBLE_PARSERS.values()

    with Session(engine) as session:
        if recreate:
            TrackAlias.truncate_cascade(session)
            print("all previous track aliases/scrobbles are truncated")
        for parser in parsers:
            print("Importing", parser.platform, "scrobbles")
            aim, aig, ask, sim, sig = parser.import_aliases_scrobbles(session)
            print(parser.platform, "stats:")
            print("Aliases created/ignored/skipped:", f"{aim}/{aig}/{ask}")
            print("Scrobbles created/skipped:", f"{sim}/{sig}")


@scrobble_app.command("augment")
def complete_cli(dry_run: bool = typer.Option(False, "--dry-run", "-d")):
    with Session(engine) as session:
        augment_aliases(session, dry_run=dry_run)
