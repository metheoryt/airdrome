import typer
from rich.console import Console
from sqlmodel import SQLModel, Session, delete

from jellyfist.cloud.apple import ingest_library, AppleScrobbleParser
from jellyfist.cloud.lastfm import LastFMScrobbleParser
from jellyfist.cloud.spotify import SpotifyScrobbleParser
from jellyfist.enums import Platform
from jellyfist.models import engine, TrackAlias
from jellyfist.normalize import deduplicate_tracks, normalize_track_names, normalize_alias_names
from jellyfist.conf import settings

app = typer.Typer(help="jellyfist CLI")
console = Console()


# create any missing tables
SQLModel.metadata.create_all(engine, checkfirst=True)


@app.command()
def ingest(recreate: bool = typer.Option(False, "--recreate", "-r")):
    console.print("[bold green]Starting ingest...[/bold green]")
    ingest_library(settings.apple_music_library_xml_filepath, recreate=recreate)
    console.print("[bold green]Data ingest completed successfully.[/bold green]")


@app.command("deduplicate")
def deduplicate_cli():
    with Session(engine) as session:
        deduplicate_tracks(session)


@app.command()
def renormalize():
    normalize_track_names()
    normalize_alias_names()


SCROBBLE_PARSERS = {
    Platform.LASTFM: LastFMScrobbleParser(settings.lastfm_scrobbles_filepath),
    Platform.APPLE: AppleScrobbleParser(settings.apple_music_play_activity_filepath),
    Platform.SPOTIFY: SpotifyScrobbleParser(settings.spotify_streaming_history_dirpath),
}


@app.command("scrobble")
def scrobble_import(
    platform: Platform | None = None, recreate: bool = typer.Option(False, "--recreate", "-r")
):
    if platform:
        parsers = [SCROBBLE_PARSERS[platform]]
    else:
        print("Importing all scrobbles")
        parsers = SCROBBLE_PARSERS.values()

    with Session(engine) as session:
        if recreate:
            session.exec(delete(TrackAlias))
            session.commit()
            print("all previous track aliases/scrobbles are deleted")
        for parser in parsers:
            print("Importing", parser.platform, "scrobbles")
            aim, aig, sim, sig = parser.import_aliases_scrobbles(session)
            print(parser.platform, "stats:")
            print("Aliases created:", aim)
            print("Aliases skipped:", aig)
            print("Scrobbles created:", sim)
            print("Scrobbles skipped:", sig)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
