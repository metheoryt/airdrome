from pathlib import Path

import typer
from rich.console import Console

from airdrome.cloud.apple.ingest import import_apple_library


apple_app = typer.Typer(help="Airdrome Apple Music CLI")
console = Console()


@apple_app.command("import")
def apple_import(
    library_xml: str = typer.Option(..., "--xml", "-x", help="Path to Apple Music Library XML file"),
    library_dir: str = typer.Option(..., "--dir", "-d", help="Path to Apple Music Library root directory"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    library_xml_path = Path(library_xml)
    if not library_xml_path.exists():
        console.print(f"Library XML file does not exist: {library_xml_path}", style="bold red")
        raise typer.Exit(code=1)
    if not library_xml_path.is_file():
        console.print(f"Library XML is not a file: {library_xml_path}", style="bold red")
        raise typer.Exit(code=1)

    library_dir_path = Path(library_dir)
    if not library_dir_path.is_dir():
        console.print(f"Library path is not a directory: {library_dir_path}", style="bold red")
        raise typer.Exit(code=1)

    import_apple_library(str(library_xml_path), str(library_dir_path), reset=reset)
