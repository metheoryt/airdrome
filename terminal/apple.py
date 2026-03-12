import typer
from rich.console import Console

from airdrome.cloud.apple.ingest import import_apple_library
from airdrome.conf import settings


# from airdrome.transfer import transfer_library


apple_app = typer.Typer(help="Airdrome Apple Music CLI")
console = Console()


@apple_app.command("import-library")
def apple_import_library(reset: bool = typer.Option(False, "--reset", "-r")):
    console.print("Starting ingest...", style="bold green")
    import_apple_library(settings.apple_music_library_xml_filepath, reset=reset)
    console.print("Data ingest completed successfully.", style="bold green")


# @apple_app.command("transfer-files")
# def apple_transfer_files():
#     transfer_library(
#         source_dir=settings.apple_music_library_dirpath,
#         target_dir_originals=settings.local_library_dirpath,
#         target_dir_copies=settings.local_library_copies_dirpath,
#     )
