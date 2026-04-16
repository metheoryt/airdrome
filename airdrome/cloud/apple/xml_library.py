import plistlib
from pathlib import Path

from rich.progress import Progress, TaskID
from sqlmodel import Session, delete, exists, select

from airdrome.console import console, make_import_progress, make_progress
from airdrome.models import Track, TrackFile, engine

from .models import ApplePlaylist, ApplePlaylistImport, ApplePlaylistTrack, AppleTrack


def get_track_full_paths(t: AppleTrack, root_dir: str) -> set[Path]:
    paths = set()

    for track_path in t.possible_locations(max_suffix=2):
        full_path = root_dir / track_path

        if full_path.exists():
            paths.add(full_path.resolve())

    return paths


def do_import_tracks(
    s: Session,
    tracks_data: dict,
    root_dir: Path,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> tuple[int, int]:
    """
    Import Apple Music tracks into the database. Returns (created, updated) Track counts.

    Testable directly — no session creation, no progress output.
    """
    created = updated = 0
    for data in tracks_data.values():
        at = AppleTrack(**data)

        apple_track = s.exec(
            select(AppleTrack).where(AppleTrack.apple_track_id == at.apple_track_id)
        ).one_or_none()
        if apple_track:
            if progress is not None:
                progress.advance(task_id)
            continue

        apple_track = at

        track_defaults = dict(
            track_n=apple_track.track_number,
            disc_n=apple_track.disc_number,
            compilation=apple_track.compilation,
            year=apple_track.year,
            duration=round(apple_track.total_time / 1000) if apple_track.total_time else None,
            loved=apple_track.loved if apple_track.loved else None,
            album_loved=apple_track.album_loved if apple_track.album_loved else None,
            rating=apple_track.rating if not apple_track.rating_computed else None,
            album_rating=apple_track.album_rating if not apple_track.album_rating_computed else None,
            date_added=apple_track.date_added,
        )
        track, track_created = Track.get_or_create(
            s,
            title=apple_track.name,
            artist=apple_track.artist,
            album=apple_track.album,
            album_artist=apple_track.album_artist,
            defaults=track_defaults,
        )
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        apple_track.track = track
        s.add(apple_track)
        s.flush()

        if not apple_track.apple_music:
            for tp in get_track_full_paths(apple_track, root_dir):
                tf, _ = TrackFile.get_or_create(s, source_path=tp, defaults=dict(track_id=track.id))
                tf.enrich()
        s.flush()

        if progress is not None:
            progress.update(task_id, advance=1, created=created, updated=updated)

    return created, updated


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
            apple_track = pl_tracks[pls_track.apple_track_id]
            if apple_track.id not in existing_track_ids:
                apt = ApplePlaylistTrack(track=apple_track, playlist=pl_db, position=pos)
                s.add(apt)
            seen.add(pls_track.apple_track_id)
        s.flush()

        if progress is not None:
            progress.advance(task_id)

    return created


def import_apple_library(xml_filename: str, root_dir: str, reset: bool = False):
    if reset:
        with Session(engine) as s:
            s.exec(delete(Track).where(exists().where(AppleTrack.track_id == Track.id)))
            s.exec(delete(ApplePlaylist))
            s.commit()
        console.print("[yellow]Apple library purged[/yellow]")

    with open(xml_filename, "rb") as f:
        plist = plistlib.load(f)

    with Session(engine) as s:
        tracks_data = plist["Tracks"]
        with make_import_progress() as progress:
            task = progress.add_task("Tracks", total=len(tracks_data), created=0, updated=0)
            created, updated = do_import_tracks(s, tracks_data, root_dir, progress=progress, task_id=task)
        console.print(f"Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]")

        playlists_data = plist["Playlists"]
        with make_progress() as progress:
            task = progress.add_task("Playlists", total=len(playlists_data))
            n_playlists = do_import_playlists(s, playlists_data, progress=progress, task_id=task)
        console.print(f"Playlists: [green]{n_playlists} new[/green]")

        s.commit()

    console.print("[green]Apple library import finished[/green]")
