from pathlib import Path

import typer
from sqlmodel import Session, update

from airdrome.conf import settings
from airdrome.console import console
from airdrome.library.organize import organize_library
from airdrome.library.scan import MusicScanner
from airdrome.models import Track, engine
from airdrome.normalize.dedup import Deduplicator
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names


library_app = typer.Typer(help="Library tools")


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
            console.print("[yellow]duplicates data reset[/yellow]")
        Deduplicator(session, filepath=settings.duplicates_filepath).run()


@library_app.command()
def renormalize():
    with Session(engine) as session:
        normalize_track_names(session)
        normalize_alias_names(session)
        normalize_track_file_names(session)
        session.commit()
