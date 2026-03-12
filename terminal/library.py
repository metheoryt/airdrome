import typer
from sqlmodel import Session, update

from airdrome.conf import settings
from airdrome.models import Track, engine
from airdrome.normalize.dedup import deduplicate_tracks
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names
from airdrome.tools.reindex import FileIndexer


library_app = typer.Typer(help="Airdrome Apple Music CLI")


@library_app.command("index")
def index_library_cli(threshold: float = typer.Option(0.4, "--threshold", "-t")):
    FileIndexer(library_path=settings.apple_music_library_dirpath, match_threshold=threshold).index_library()


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
