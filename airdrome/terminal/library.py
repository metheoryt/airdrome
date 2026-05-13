from pathlib import Path

import typer
from sqlalchemy import update

from airdrome.conf import settings
from airdrome.console import console
from airdrome.library.organize import organize_library
from airdrome.library.scan import MusicScanner
from airdrome.library.unify import do_unify
from airdrome.models import Track
from airdrome.normalize.dedup import Deduplicator, DeduplicatorUI, auto_deduplicate
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names

from .state import AppState


library_app = typer.Typer(help="Library tools")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")


@library_app.command("organize")
def library_organize(
    ctx: typer.Context,
    copy: bool = typer.Option(False, "--copy", "-c"),
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    organize_library(state.session, dst_dir=settings.library_dir, copy=copy, reset=reset)


@library_app.command("scan")
def scan_folder(
    ctx: typer.Context,
    folder_path: str = typer.Argument(help="Folder path to scan."),
    threshold: float = typer.Option(0.4, "--threshold", "-t", help="Existing tracks matching threshold."),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    MusicScanner(target_path=Path(folder_path), match_threshold=threshold).run(state.session)


@library_app.command("deduplicate")
def deduplicate_cli(
    ctx: typer.Context,
    match: str = typer.Option("", "--match", help="Filter by a substring"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    state: AppState = ctx.obj
    if reset:
        state.session.execute(update(Track).values(canon_id=None))
        console.print("[yellow]duplicates data reset[/yellow]")
    Deduplicator(
        state.session,
        filepath=settings.duplicates_filepath,
        partial_match=match,
    ).run()


@library_app.command("auto-deduplicate")
def auto_deduplicate_cli(
    ctx: typer.Context,
    no_artist: bool = typer.Option(False, "--no-artist", help="Exclude artist from matching."),
    no_album_artist: bool = typer.Option(
        False, "--no-album-artist", help="Exclude album artist from matching."
    ),
    no_album: bool = typer.Option(False, "--no-album", help="Exclude album from matching."),
    no_track_n: bool = typer.Option(False, "--no-track-n", help="Exclude track number from matching."),
    no_disc_n: bool = typer.Option(False, "--no-disc-n", help="Exclude disc number from matching."),
    no_duration: bool = typer.Option(False, "--no-duration", help="Exclude duration bucket from matching."),
    no_year: bool = typer.Option(False, "--no-year", help="Exclude year from matching."),
    dry_run: bool = _DRY_RUN,
):
    """Rebuild Track.canon_id from this flag-set + duplicates.json overrides.

    Every run is a clean slate: all canon_ids are reset, the chosen flag-set
    decides auto groupings, then manual choices from duplicates.json are
    layered on top. Re-run with different flags to experiment freely.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run

    flags = {
        "with_artist": not no_artist,
        "with_album_artist": not no_album_artist,
        "with_album": not no_album,
        "with_track_n": not no_track_n,
        "with_disc_n": not no_disc_n,
        "with_duration": not no_duration,
        "with_year": not no_year,
    }
    result = auto_deduplicate(state.session, **flags)
    for group in result.groups:
        canons = [None] + [group[0].id] * (len(group) - 1)
        console.print(DeduplicatorUI.compose_table("auto-dedup", group, canons))

    style = "yellow" if dry_run else "green"
    suffix = " (dry run, will roll back)" if dry_run else ""
    console.print(
        f"[{style}]{result.auto_twins} twin(s) across {len(result.groups)} group(s)"
        f" + {result.manual_changes} manual override(s) from duplicates.json"
        f"{suffix}.[/{style}]"
    )


@library_app.command("unify")
def library_unify(
    ctx: typer.Context,
    reset: bool = typer.Option(False, "--reset", "-r", help="Delete and rebuild all canonical playlists."),
    dry_run: bool = _DRY_RUN,
):
    """Create canonical Track and Playlist records from all imported platform data."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    do_unify(state.session, reset_playlists=reset)
    console.print("[bold green]Unify complete[/bold green]")


@library_app.command()
def renormalize(ctx: typer.Context, dry_run: bool = _DRY_RUN):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    normalize_track_names(state.session)
    normalize_alias_names(state.session)
    normalize_track_file_names(state.session)
