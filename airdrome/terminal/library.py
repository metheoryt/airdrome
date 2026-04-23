from pathlib import Path

import typer
from sqlmodel import Session, update

from airdrome.conf import settings
from airdrome.console import console
from airdrome.library.organize import organize_library
from airdrome.library.scan import MusicScanner
from airdrome.models import Track, engine
from airdrome.normalize.dedup import Deduplicator, DeduplicatorUI, auto_deduplicate
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
def deduplicate_cli(
    match: str = typer.Option("", "--match", help="Filter by a substring"),
    reset: bool = typer.Option(False, "--reset", "-r"),
):
    with Session(engine) as session:
        if reset:
            session.exec(update(Track).values(canon_id=None))
            console.print("[yellow]duplicates data reset[/yellow]")
        Deduplicator(
            session,
            filepath=settings.duplicates_filepath,
            partial_match=match,
        ).run()


@library_app.command("auto-deduplicate")
def auto_deduplicate_cli(
    no_artist: bool = typer.Option(False, "--no-artist", help="Exclude artist from matching."),
    no_album_artist: bool = typer.Option(
        False, "--no-album-artist", help="Exclude album artist from matching."
    ),
    no_album: bool = typer.Option(False, "--no-album", help="Exclude album from matching."),
    no_track_n: bool = typer.Option(False, "--no-track-n", help="Exclude track number from matching."),
    no_disc_n: bool = typer.Option(False, "--no-disc-n", help="Exclude disc number from matching."),
    no_duration: bool = typer.Option(False, "--no-duration", help="Exclude duration bucket from matching."),
    no_year: bool = typer.Option(False, "--no-year", help="Exclude year from matching."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Report matches without applying changes."),
):
    with Session(engine) as session:
        groups = auto_deduplicate(
            session,
            with_artist=not no_artist,
            with_album_artist=not no_album_artist,
            with_album=not no_album,
            with_track_n=not no_track_n,
            with_disc_n=not no_disc_n,
            with_duration=not no_duration,
            with_year=not no_year,
            dry_run=dry_run,
        )
        twin_count = sum(len(g) - 1 for g in groups)
        for group in groups:
            canons = [None] + [group[0].id] * (len(group) - 1)
            console.print(DeduplicatorUI.compose_table("auto-dedup", group, canons))
    if dry_run:
        console.print(
            f"[yellow]{twin_count} track(s) / {len(groups)} group(s) "
            f"would be marked as twins (dry run).[/yellow]"
        )
    else:
        console.print(f"[green]{twin_count} track(s) / {len(groups)} group(s) auto-deduplicated.[/green]")


@library_app.command()
def renormalize():
    with Session(engine) as session:
        normalize_track_names(session)
        normalize_alias_names(session)
        normalize_track_file_names(session)
        session.commit()
