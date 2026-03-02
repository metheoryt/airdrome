from typing import Literal

import typer
from rich.console import Console
from sqlmodel import SQLModel, Session, delete

from jellyfist.cloud.apple import ingest_library, AppleScrobbleParser, transfer_library
from jellyfist.cloud.lastfm import LastFMScrobbleParser
from jellyfist.cloud.spotify import SpotifyScrobbleParser
from jellyfist.conf import settings
from jellyfist.enums import Platform
from jellyfist.loco.navidrome import sync_playlists_to_navi, sync_tracks_plays_to_navi
from jellyfist.models import engine, TrackAlias
from jellyfist.normalize import deduplicate_tracks, normalize_track_names, normalize_alias_names
from jellyfist.scrobbles.matcher import AliasToTrackMatcher, TrackToAliasMatcher

app = typer.Typer(help="JellyFist CLI")
navidrome_app = typer.Typer(help="JellyFist Navidrome CLI")
app.add_typer(navidrome_app, name="navi")
console = Console()


# create any missing tables
SQLModel.metadata.create_all(engine, checkfirst=True)


@navidrome_app.command("playlists")
def navidrome_playlists(username: str):
    console.print("[bold green]Syncing jellyfist playlists to Navidrome[/bold green]")
    sync_playlists_to_navi(username)
    console.print("[bold green]Sync completed[/bold green]")


@navidrome_app.command("tracks")
def navidrome_tracks(username: str):
    console.print("[bold green]Syncing jellyfist tracks and scrobbles to Navidrome[/bold green]")
    sync_tracks_plays_to_navi(username)
    console.print("[bold green]Sync completed[/bold green]")


@app.command()
def ingest(recreate: bool = typer.Option(False, "--recreate", "-r")):
    console.print("[bold green]Starting ingest...[/bold green]")
    ingest_library(settings.apple_music_library_xml_filepath, recreate=recreate)
    console.print("[bold green]Data ingest completed successfully.[/bold green]")


@app.command()
def transfer():
    transfer_library(
        source_dir=settings.apple_music_library_dirpath,
        target_dir_originals=settings.local_library_dirpath,
        target_dir_copies=settings.local_library_copies_dirpath,
    )


@app.command("match")
def match_cli(
    mode: Literal["track2alias", "alias2track"], reset: bool = typer.Option(False, "--reset", "-r")
):
    if mode == "track2alias":
        print("Matching tracks to aliases")
        TrackToAliasMatcher.match_all(reset=reset)
    elif mode == "alias2track":
        print("Matching aliases to tracks")
        AliasToTrackMatcher.match_all(reset=reset)
    else:
        raise ValueError(f"Unknown mode: {mode}")


@app.command("deduplicate")
def deduplicate_cli():
    with Session(engine) as session:
        deduplicate_tracks(session)


@app.command()
def renormalize():
    with Session(engine) as session:
        normalize_track_names(session)
        normalize_alias_names(session)
        session.commit()


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
