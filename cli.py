from typing import Literal

import typer
from rich.console import Console
from sqlmodel import SQLModel, Session, delete

from jellyfist.cloud.apple import import_apple_library, AppleScrobbleParser
from jellyfist.cloud.lastfm import LastFMScrobbleParser
from jellyfist.cloud.spotify import SpotifyScrobbleParser
from jellyfist.conf import settings
from jellyfist.enums import Platform

# from jellyfist.loco.navidrome import sync_playlists_to_navi, sync_tracks_plays_to_navi
from jellyfist.models import engine, TrackAlias
from jellyfist.normalize.names import normalize_track_names, normalize_alias_names
from jellyfist.normalize.dedup import deduplicate_tracks
from jellyfist.scrobbles.matcher import AliasToTrackMatcher, TrackToAliasMatcher
from jellyfist.transfer import transfer_library

app = typer.Typer(help="Airdrome CLI")
apple_app = typer.Typer(help="Airdrome Apple Music CLI")
navidrome_app = typer.Typer(help="Airdrome Navidrome CLI")
app.add_typer(navidrome_app, name="navi")
app.add_typer(apple_app, name="apple")
console = Console()

# create any missing tables
SQLModel.metadata.create_all(engine, checkfirst=True)


@apple_app.command("import-library")
def apple_import_library(reset: bool = typer.Option(False, "--reset", "-r")):
    console.print("[bold green]Starting ingest...[/bold green]")
    import_apple_library(settings.apple_music_library_xml_filepath, reset=reset)
    console.print("[bold green]Data ingest completed successfully.[/bold green]")


#
# TO ENSURE AREA
#


# @navidrome_app.command("sync-playlists")
# def navidrome_playlists(username: str):
#     console.print("[bold green]Syncing jellyfist playlists to Navidrome[/bold green]")
#     sync_playlists_to_navi(username)
#     console.print("[bold green]Sync completed[/bold green]")
#
#
# @navidrome_app.command("sync-tracks")
# def navidrome_tracks(username: str, reset: bool = typer.Option(False, "--reset", "-r")):
#     console.print("[bold green]Syncing jellyfist tracks and scrobbles to Navidrome[/bold green]")
#     sync_tracks_plays_to_navi(username, reset)
#     console.print("[bold green]Sync completed[/bold green]")


@apple_app.command("transfer-files")
def apple_transfer_files():
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
def scrobble_import(platform: Platform | None = None, recreate: bool = typer.Option(False, "--reset", "-r")):
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
