from pathlib import Path

import typer

from airdrome.cloud.apple.media_services import import_apple_media_services
from airdrome.cloud.apple.xml_library import import_apple_library
from airdrome.console import console


apple_app = typer.Typer(help="Apple Music tools")


@apple_app.command("collect-xml")
def apple_collect_xml(
    library_xml: str = typer.Option(..., "--xml", "-x", help="Path to Apple Music Library XML file"),
    library_dir: str = typer.Option(..., "--dir", "-d", help="Path to Apple Music Library root directory"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    library_xml_path = Path(library_xml)
    if not library_xml_path.exists() or not library_xml_path.is_file():
        console.print(f"Library XML file not found: {library_xml_path}", style="bold red")
        raise typer.Exit(code=1)

    library_dir_path = Path(library_dir)
    if not library_dir_path.is_dir():
        console.print(f"Library path is not a directory: {library_dir_path}", style="bold red")
        raise typer.Exit(code=1)

    import_apple_library(str(library_xml_path), str(library_dir_path), reset=reset)


@apple_app.command("collect-media-services")
def apple_collect_media_services(
    activity_dir: str = typer.Option(
        ...,
        "--activity-dir",
        "-a",
        help="Path to 'Apple Music Activity' folder from Apple Media Services export",
    ),
    library_dir: str = typer.Option(..., "--dir", "-d", help="Path to Apple Music Library root directory"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    activity_path = Path(activity_dir)
    if not activity_path.is_dir():
        console.print(f"Activity directory not found: {activity_path}", style="bold red")
        raise typer.Exit(code=1)

    library_dir_path = Path(library_dir)
    if not library_dir_path.is_dir():
        console.print(f"Library path is not a directory: {library_dir_path}", style="bold red")
        raise typer.Exit(code=1)

    import_apple_media_services(str(activity_path), str(library_dir_path), reset=reset)
