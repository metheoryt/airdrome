from pathlib import Path

import typer
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.ingest import BY_NAME, DataKind, Importer, detect
from airdrome.library.unify import do_unify
from airdrome.migrations import upgrade_to_head
from airdrome.models import engine
from airdrome.scrobbles.augment_aliases import augment_aliases
from airdrome.scrobbles.copy_plays import copy_plays
from airdrome.scrobbles.match_aliases import match_aliases

from .library import library_app
from .navidrome import navidrome_app
from .state import AppState


app = typer.Typer(help="Airdrome CLI")
app.add_typer(library_app, name="library")
app.add_typer(navidrome_app, name="navidrome")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
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
            console.print("[dim]dry run - no changes committed[/dim]")
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
    dry_run: bool = _DRY_RUN,
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


@app.command("resolve")
def resolve(
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
    dry_run: bool = _DRY_RUN,
):
    """Build the canonical graph from everything imported.

    Runs the full post-import resolution in dependency order: unify source tracks/playlists into
    canonical records, then augment, fuzzy-match, and materialize scrobbles into play history.
    Requires all imports to be done first (see the file-binding / playlist-resolution notes in
    library.unify) and is idempotent — re-running only fills gaps.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run
    do_unify(state.session, merge_playlists=merge_playlists, rebuild_playlists=rebuild_playlists)
    augment_aliases(state.session)
    match_aliases(state.session, threshold=threshold)
    copy_plays(state.session)
    console.print("[bold green]Resolve complete[/bold green]")


@library_app.callback(invoke_without_command=True)
@navidrome_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
