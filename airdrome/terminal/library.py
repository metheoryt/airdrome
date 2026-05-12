from pathlib import Path

import typer
from sqlalchemy import update

from airdrome.conf import settings
from airdrome.console import console
from airdrome.library.organize import organize_library
from airdrome.library.scan import MusicScanner
from airdrome.library.unify import do_unify
from airdrome.models import Track
from airdrome.normalize.dedup import Deduplicator, DeduplicatorUI, compute_auto_dedup_groups
from airdrome.normalize.dedup_history import format_flags, load_history, record_run
from airdrome.normalize.materialize import materialize
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


def _print_materialize_stats(stats, dry_run: bool) -> None:
    style = "yellow" if dry_run else "green"
    suffix = " (dry run, will roll back)" if dry_run else ""
    console.print(
        f"[{style}]Materialized {stats.auto_twins} twin(s) across {stats.auto_components} "
        f"component(s) from {stats.history_entries} history entry(ies)"
        f" + {stats.manual_changes} manual change(s)"
        f"{f' (+{stats.chain_rewrites} chain rewrite(s))' if stats.chain_rewrites else ''}"
        f"{suffix}.[/{style}]"
    )


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
    else:
        # Bring the DB into sync with history + duplicates.json before the UI
        # reads current canon_id state into its pages.
        materialize(state.session)
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
    """Record a flag-set into auto-dedup history and re-materialize the world.

    The DB's canon_id state is treated as a materialized view of
    (auto-dedup history + duplicates.json). Recording a new flag-set adds
    edges; all history is then collapsed into connected components via
    union-find, with the earliest-sorted track in each component picked as
    canon. Order of recording does not affect the final state.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run

    history_path = settings.auto_dedup_history_filepath
    history = load_history(history_path)
    if history:
        console.print("[dim]Previous auto-dedup runs:[/dim]")
        for entry in history:
            console.print(
                f"[dim]  {entry['ran_at'][:16]}  {format_flags(entry['flags'])}  "
                f"→  {entry['groups']} groups, {entry['twins']} twins[/dim]"
            )

    flags = {
        "with_artist": not no_artist,
        "with_album_artist": not no_album_artist,
        "with_album": not no_album,
        "with_track_n": not no_track_n,
        "with_disc_n": not no_disc_n,
        "with_duration": not no_duration,
        "with_year": not no_year,
    }
    groups = compute_auto_dedup_groups(state.session, **flags)
    twin_count = sum(len(g) - 1 for g in groups)
    for group in groups:
        canons = [None] + [group[0].id] * (len(group) - 1)
        console.print(DeduplicatorUI.compose_table("auto-dedup", group, canons))

    if not groups:
        console.print("[dim]This flag-set produces no groups; nothing recorded.[/dim]")
        return

    if any(entry["flags"] == flags for entry in history):
        console.print(
            "[dim]This flag-set is already in history; nothing recorded. "
            "Run 'library deduplicate-replay' to rebuild the DB from sources.[/dim]"
        )
        return

    if dry_run:
        console.print(
            f"[yellow](dry run) Would record {len(groups)} group(s) / {twin_count} twin(s) "
            f"and re-materialize.[/yellow]"
        )
        return

    record_run(history_path, flags, len(groups), twin_count)
    console.print(f"[dim]Recorded run in {history_path}[/dim]")
    stats = materialize(state.session)
    _print_materialize_stats(stats, dry_run=False)


@library_app.command("deduplicate-replay")
def deduplicate_replay_cli(
    ctx: typer.Context,
    dry_run: bool = _DRY_RUN,
):
    """Rebuild the DB's canon_id state from auto-dedup history + duplicates.json."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    stats = materialize(state.session)
    _print_materialize_stats(stats, dry_run=dry_run)


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
