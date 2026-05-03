from pathlib import Path

import typer

from airdrome.cloud.apple.media_services import import_apple_media_services
from airdrome.cloud.apple.xml_library import import_apple_library
from airdrome.console import console

from .state import AppState


apple_app = typer.Typer(help="Apple Music tools")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")


@apple_app.command("collect-xml")
def apple_collect_xml(
    ctx: typer.Context,
    library_xml: str = typer.Option(..., "--xml", "-x", help="Path to Apple Music Library XML file"),
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    library_xml_path = Path(library_xml)
    if not library_xml_path.exists() or not library_xml_path.is_file():
        console.print(f"Library XML file not found: {library_xml_path}", style="bold red")
        raise typer.Exit(code=1)

    state: AppState = ctx.obj
    state.dry_run = dry_run
    import_apple_library(state.session, str(library_xml_path), reset=reset)


@apple_app.command("collect-media-services")
def apple_collect_media_services(
    ctx: typer.Context,
    path: str = typer.Option(
        ...,
        "--path",
        "-p",
        help="Apple Media Services export: .zip file or extracted directory",
    ),
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    p = Path(path)
    if not p.exists():
        console.print(f"Path not found: {p}", style="bold red")
        raise typer.Exit(code=1)

    state: AppState = ctx.obj
    state.dry_run = dry_run
    import_apple_media_services(state.session, str(p), reset=reset)
