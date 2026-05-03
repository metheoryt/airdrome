import plistlib

from rich.progress import Progress, TaskID
from sqlmodel import Session, delete, select

from airdrome.console import console, make_import_progress, make_progress

from .models import ApplePlaylist, ApplePlaylistTrack, AppleTrack
from .schemas import ApplePlaylistImport


def do_import_tracks(
    s: Session,
    tracks_data: dict,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> int:
    """
    Import Apple Music tracks into the database. Returns number of new AppleTrack records created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for data in tracks_data.values():
        at = AppleTrack(**data)

        if s.exec(select(AppleTrack).where(AppleTrack.apple_track_id == at.apple_track_id)).one_or_none():
            if progress is not None:
                progress.advance(task_id)
            continue

        s.add(at)
        s.flush()
        created += 1

        if progress is not None:
            progress.update(task_id, advance=1, created=created)

    return created


def do_import_playlists(
    s: Session,
    playlists_data: list,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> int:
    """
    Import Apple Music playlists into the database. Returns number of new playlists created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for pl in playlists_data:
        pl_import = ApplePlaylistImport(**pl)

        if progress is not None:
            progress.update(task_id, description=pl_import.name)

        if pl_import.smart_info:
            if progress is not None:
                progress.advance(task_id)
            continue

        pl_db = s.exec(
            select(ApplePlaylist).where(ApplePlaylist.playlist_id == pl_import.playlist_id)
        ).one_or_none()
        if not pl_db:
            pl_db = ApplePlaylist.model_validate(pl_import)
            s.add(pl_db)
            s.flush()
            created += 1

        existing_track_ids = {
            link.track_id
            for link in s.exec(select(ApplePlaylistTrack).where(ApplePlaylistTrack.playlist_id == pl_db.id))
        }
        seen = set()
        pl_track_ids = [v.apple_track_id for v in pl_import.items]
        pl_tracks = {
            t.apple_track_id: t
            for t in s.exec(select(AppleTrack).where(AppleTrack.apple_track_id.in_(pl_track_ids)))
        }
        pos = 0
        for pls_track in pl_import.items:
            if pls_track.apple_track_id in seen:
                continue

            pos += 1
            apple_track = pl_tracks.get(pls_track.apple_track_id)
            if apple_track is None:
                continue
            if apple_track.id not in existing_track_ids:
                apt = ApplePlaylistTrack(track=apple_track, playlist=pl_db, position=pos)
                s.add(apt)
            seen.add(pls_track.apple_track_id)
        s.flush()

        if progress is not None:
            progress.advance(task_id)

    return created


def import_apple_library(s: Session, xml_filename: str, reset: bool = False):
    if reset:
        s.exec(delete(ApplePlaylist))
        s.exec(delete(AppleTrack))
        s.flush()
        console.print("[yellow]Apple library purged[/yellow]")

    with open(xml_filename, "rb") as f:
        plist = plistlib.load(f)

    tracks_data = plist["Tracks"]
    with make_import_progress() as progress:
        task = progress.add_task("Tracks", total=len(tracks_data), created=0, updated=0)
        created = do_import_tracks(s, tracks_data, progress=progress, task_id=task)
    console.print(f"Tracks: [green]{created} new[/green]")

    playlists_data = plist["Playlists"]
    with make_progress() as progress:
        task = progress.add_task("Playlists", total=len(playlists_data))
        n_playlists = do_import_playlists(s, playlists_data, progress=progress, task_id=task)
    console.print(f"Playlists: [green]{n_playlists} new[/green]")

    console.print(
        "[green]Apple library import finished. Run `library unify` to create canonical records.[/green]"
    )
