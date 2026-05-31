from pathlib import Path

import typer
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.ingest import BY_NAME, DataKind, detect
from airdrome.migrations import upgrade_to_head
from airdrome.models import engine

from .library import library_app
from .navidrome import navidrome_app
from .scrobble import scrobble_app
from .state import AppState


app = typer.Typer(help="Airdrome CLI")
app.add_typer(library_app, name="library")
app.add_typer(scrobble_app, name="scrobble")
app.add_typer(navidrome_app, name="navidrome")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        return
    upgrade_to_head()
    session = ctx.with_resource(Session(engine))
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


@app.command("import")
def import_(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, help="File or folder to import"),
    as_: str = typer.Option(
        None, "--as", help=f"Force a source instead of auto-detecting: {', '.join(BY_NAME)}"
    ),
    no_tracks: bool = typer.Option(False, "--no-tracks", help="Skip importing tracks"),
    no_playlists: bool = typer.Option(False, "--no-playlists", help="Skip importing playlists"),
    no_scrobbles: bool = typer.Option(False, "--no-scrobbles", help="Skip importing scrobbles"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution."),
):
    """Auto-detect the source at PATH and import its tracks, playlists, and scrobbles."""
    state: AppState = ctx.obj
    state.dry_run = dry_run

    if as_ is not None:
        if as_ not in BY_NAME:
            console.print(f"[red]Unknown source '{as_}'. Choose from: {', '.join(BY_NAME)}[/red]")
            raise typer.Exit(1)
        importer_cls = BY_NAME[as_]
    else:
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
        importer_cls = matches[0]

    wanted = DataKind(0)
    if not no_tracks:
        wanted |= DataKind.TRACKS
    if not no_playlists:
        wanted |= DataKind.PLAYLISTS
    if not no_scrobbles:
        wanted |= DataKind.SCROBBLES

    kinds = importer_cls.provides & wanted
    if not kinds:
        console.print(
            f"[yellow]{importer_cls.name} provides nothing matching your filters; nothing to do.[/yellow]"
        )
        return

    console.print(
        f"[bold]Importing {importer_cls.label}[/bold] [dim]({', '.join(k.name.lower() for k in kinds)})[/dim]"
    )
    importer_cls(path).ingest(state.session, kinds)


@library_app.callback(invoke_without_command=True)
@scrobble_app.callback(invoke_without_command=True)
@navidrome_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
