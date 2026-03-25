from pathlib import Path

import typer
from rich.console import Console
from sqlmodel import Session, update

from airdrome.cloud.apple.ingest import import_apple_library
from airdrome.conf import settings
from airdrome.library.organize import organize_library
from airdrome.library.scan import MusicScanner
from airdrome.models import Track, engine
from airdrome.normalize.dedup import deduplicate_tracks
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names


library_app = typer.Typer(help="Library tools")
console = Console()


@library_app.command("import-apple")
def library_import_apple(
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


@library_app.command("organize")
def library_organize(
    copy: bool = typer.Option(False, "--copy", "-c"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    organize_library(dst_dir=settings.library_dir, copy=copy, reset=reset)


@library_app.command("scan")
def scan_folder(
    folder_path: str = typer.Argument(help="Folder path to scan."),
    threshold: float = typer.Option(0.4, "--threshold", "-t", help="Existing tracks matching threshold."),
):
    MusicScanner(target_path=Path(folder_path), match_threshold=threshold).run()


@library_app.command("deduplicate")
def deduplicate_cli(reset: bool = typer.Option(False, "--reset", "-r")):
    with Session(engine) as session:
        if reset:
            session.exec(update(Track).values(canon_id=None))
            print("Duplicates data reset")

        deduplicate_tracks(session)


@library_app.command()
def renormalize():
    with Session(engine) as session:
        normalize_track_names(session)
        normalize_alias_names(session)
        normalize_track_file_names(session)
        session.commit()
