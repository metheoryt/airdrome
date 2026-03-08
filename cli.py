import typer
from rich.console import Console
from sqlmodel import SQLModel, Session, update

from airdrome.cloud.apple.ingest import import_apple_library
from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.conf import settings
from airdrome.enums import Platform

# from airdrome.loco.navidrome import sync_playlists_to_navi, sync_tracks_plays_to_navi
from airdrome.models import engine, TrackAlias, Track
from airdrome.normalize.dedup import deduplicate_tracks
from airdrome.normalize.names import normalize_track_names, normalize_alias_names, normalize_track_file_names
from airdrome.scrobbles.match_aliases import match_aliases
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.tools.reindex import index_library

# from airdrome.transfer import transfer_library

app = typer.Typer(help="Airdrome CLI")

apple_app = typer.Typer(help="Airdrome Apple Music CLI")
app.add_typer(apple_app, name="apple")

navidrome_app = typer.Typer(help="Airdrome Navidrome CLI")
app.add_typer(navidrome_app, name="navi")

scrobble_app = typer.Typer(help="Airdrome scrobbles CLI")
app.add_typer(scrobble_app, name="scrobble")

console = Console()

# create any missing tables
SQLModel.metadata.create_all(engine, checkfirst=True)


@app.command("index")
def index_library_cli():
    index_library(settings.apple_music_library_dirpath)


@app.command("deduplicate")
def deduplicate_cli(reset: bool = typer.Option(False, "--reset", "-r")):
    with Session(engine) as session:
        if reset:
            session.exec(update(Track).values(canon_id=None))
            print("Duplicates data reset")

        deduplicate_tracks(session)


@app.command()
def renormalize():
    with Session(engine) as session:
        normalize_track_names(session)
        normalize_alias_names(session)
        normalize_track_file_names(session)
        session.commit()


@apple_app.command("import-library")
def apple_import_library(reset: bool = typer.Option(False, "--reset", "-r")):
    console.print("Starting ingest...", style="bold green")
    import_apple_library(settings.apple_music_library_xml_filepath, reset=reset)
    console.print("Data ingest completed successfully.", style="bold green")


@scrobble_app.command("match")
def match_cli(
    reset: bool = typer.Option(False, "--reset", "-r"), dry_run: bool = typer.Option(False, "--dry-run", "-d")
):
    match_aliases(reset=reset, dry_run=dry_run)


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


#
# TO ENSURE AREA
#


# @navidrome_app.command("sync-playlists")
# def navidrome_playlists(username: str):
#     console.print("[bold green]Syncing airdrome playlists to Navidrome[/bold green]")
#     sync_playlists_to_navi(username)
#     console.print("[bold green]Sync completed[/bold green]")
#
#
# @navidrome_app.command("sync-tracks")
# def navidrome_tracks(username: str, reset: bool = typer.Option(False, "--reset", "-r")):
#     console.print("[bold green]Syncing airdrome tracks and scrobbles to Navidrome[/bold green]")
#     sync_tracks_plays_to_navi(username, reset)
#     console.print("[bold green]Sync completed[/bold green]")


# @apple_app.command("transfer-files")
# def apple_transfer_files():
#     transfer_library(
#         source_dir=settings.apple_music_library_dirpath,
#         target_dir_originals=settings.local_library_dirpath,
#         target_dir_copies=settings.local_library_copies_dirpath,
#     )


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
