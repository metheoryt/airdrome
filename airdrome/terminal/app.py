from pathlib import Path

import typer
from sqlalchemy.orm import Session

from airdrome.console import console, done, set_verbosity, step
from airdrome.ingest import BY_NAME, DataKind, Importer, detect
from airdrome.library.unify import do_unify
from airdrome.migrations import upgrade_to_head
from airdrome.models import engine
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.copy_plays import copy_plays
from airdrome.scrobbles.match_aliases import match_aliases

from . import pipeline
from .maint import maint_app
from .navi import navi_app
from .options import DRY_RUN
from .state import AppState
from .status import status
from .sync import sync_app


_HELP = """Airdrome — migrate your music library and listening history into Navidrome.

\b
Typical flow (run in order):
  import <path>...   ingest exports & music folders
  land               build the canonical library graph
  organize           move/copy files into LIBRARY_DIR
  dedup              collapse duplicate tracks
  sync all           reconcile playlists across sources & Navidrome
  navi push          sync play counts & ratings into Navidrome

Run `status` anytime for a read-only snapshot of config and pipeline progress.
Every write command is idempotent and takes --dry-run/-n. Run any command with --help."""

app = typer.Typer(help=_HELP)
app.command("status")(status)
pipeline.register(app)
app.add_typer(sync_app, name="sync")
app.add_typer(navi_app, name="navi")
app.add_typer(maint_app, name="maint")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show per-item detail (file picks, misses)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress non-essential output."),
):
    """Open the DB session shared by every subcommand and commit (or roll back) on exit."""
    set_verbosity(1 if verbose else -1 if quiet else 0)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        return
    # `status` is a read-only diagnostic that opens its own (defensive) session, so it can
    # report an unreachable DB or un-applied migrations instead of crashing in the setup below.
    if ctx.invoked_subcommand == "status":
        return
    upgrade_to_head()
    # expire_on_commit=False keeps ORM objects populated after a commit. The
    # interactive deduplicator commits repeatedly within one session; without
    # this, the second commit re-loads every track one-by-one to recompute
    # duplicate_hash (an N+1 storm that looks like a freeze on a large library).
    session = ctx.with_resource(Session(engine, expire_on_commit=False))
    ctx.obj = AppState(session=session, dry_run=False)

    def _finalize():
        if ctx.obj.dry_run:
            session.rollback()
            console.print("[yellow]Dry run — rolled back; nothing was committed.[/yellow]")
        else:
            try:
                session.commit()
            except Exception:
                session.rollback()

    ctx.call_on_close(_finalize)


def _resolve_importer(path: Path, as_: str | None) -> type[Importer]:
    """Pick the Importer class for ``path``: honor ``--as``, else auto-detect. Exits on failure."""
    if as_ is not None:
        if as_ not in BY_NAME:
            console.print(f"[red]Unknown source '{as_}'. Choose from: {', '.join(BY_NAME)}[/red]")
            raise typer.Exit(1)
        return BY_NAME[as_]

    matches = detect(path)
    if not matches:
        console.print(
            f"[red]Couldn't recognize {path}.[/red] Force a source with --as ({', '.join(BY_NAME)})."
        )
        raise typer.Exit(1)
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        console.print(f"[red]Ambiguous: {path} matched {names}.[/red] Disambiguate with --as.")
        raise typer.Exit(1)
    return matches[0]


@app.command("import")
def import_(
    ctx: typer.Context,
    paths: list[Path] = typer.Argument(..., exists=True, help="One or more files/folders to import"),
    as_: str = typer.Option(
        None,
        "--as",
        help=f"Force a source instead of auto-detecting (applies to every path): {', '.join(BY_NAME)}",
    ),
    no_tracks: bool = typer.Option(False, "--no-tracks", help="Skip importing tracks"),
    no_playlists: bool = typer.Option(False, "--no-playlists", help="Skip importing playlists"),
    no_scrobbles: bool = typer.Option(False, "--no-scrobbles", help="Skip importing scrobbles"),
    dry_run: bool = DRY_RUN,
):
    """Auto-detect the source at each PATH and import its tracks, playlists, and scrobbles."""
    state: AppState = ctx.obj
    state.dry_run = dry_run

    wanted = DataKind(0)
    if not no_tracks:
        wanted |= DataKind.TRACKS
    if not no_playlists:
        wanted |= DataKind.PLAYLISTS
    if not no_scrobbles:
        wanted |= DataKind.SCROBBLES

    # Resolve every importer up front so an unrecognized/ambiguous path fails before we write anything.
    plan = [(path, _resolve_importer(path, as_)) for path in paths]

    for path, importer_cls in plan:
        kinds = importer_cls.provides & wanted
        if not kinds:
            console.print(
                f"[yellow]{importer_cls.name} provides nothing matching your filters "
                f"for {path}; skipping.[/yellow]"
            )
            continue
        console.print(
            f"[bold]Importing {importer_cls.label}[/bold] "
            f"[dim]{path} ({', '.join(k.name.lower() for k in kinds)})[/dim]"
        )
        importer_cls(path).ingest(state.session, kinds)


@app.command("land")
def land(
    ctx: typer.Context,
    threshold: float = typer.Option(0.4, "--threshold", "-t", help="Fuzzy alias-match similarity threshold."),
    merge_playlists: bool = typer.Option(
        False,
        "--merge-playlists",
        "-m",
        help="Merge same-name playlists into one canonical (newest anchors, duplicate tracks skipped).",
    ),
    rebuild_playlists: bool = typer.Option(
        False,
        "--rebuild-playlists",
        help="Drop all canonical playlists first and rebuild from source. Also discards backend-sync links.",
    ),
    dry_run: bool = DRY_RUN,
):
    """Build the canonical graph from everything imported.

    Runs the full post-import resolution in dependency order: unify source tracks/playlists into
    canonical records, then augment, fuzzy-match, and materialize scrobbles into play history.
    Requires all imports to be done first (see the file-binding / playlist-resolution notes in
    library.unify) and is idempotent — re-running only fills gaps.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run
    step(1, 4, "Unify source data into canonical records")
    do_unify(state.session, merge_playlists=merge_playlists, rebuild_playlists=rebuild_playlists)
    step(2, 4, "Augment aliases with missing artist/album")
    augment_aliases(state.session)
    step(3, 4, "Match aliases to canonical tracks")
    match_aliases(state.session, threshold=threshold)
    step(4, 4, "Copy plays into history")
    copy_plays(state.session)
    done("Resolve complete")


@navi_app.callback(invoke_without_command=True)
@maint_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
    """Show the sub-app's help when invoked without a subcommand."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
