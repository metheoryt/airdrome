import json
import zipfile
from datetime import datetime
from pathlib import Path

from rich.progress import Progress, TaskID
from sqlmodel import Session, delete, select

from airdrome.console import console, make_import_progress, make_progress
from airdrome.models import Track, TrackFile, engine

from .models import AppleMediaServicesPlaylist, AppleMediaServicesPlaylistTrack, AppleMediaServicesTrack


_SKIP_PLAYLIST_TYPES = {"Smart Playlist", "Genius Mix", "Genius Playlist", "Folder"}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_json_zip(zip_path: Path) -> list:
    with zipfile.ZipFile(zip_path) as z:
        with z.open(z.namelist()[0]) as f:
            return json.load(f)


def _get_track_full_paths(
    ms_track: AppleMediaServicesTrack, root_dir: Path, max_suffix: int = 2
) -> set[Path]:
    paths = set()
    for rel_path in ms_track.possible_locations(max_suffix=max_suffix):
        full_path = root_dir / rel_path
        if full_path.exists():
            paths.add(full_path.resolve())
    return paths


def do_import_ms_tracks(
    s: Session,
    tracks: list[dict],
    root_dir: Path,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> tuple[int, int]:
    """Import Apple Media Services tracks. Returns (created, updated) Track counts."""
    created = updated = 0
    for item in tracks:
        track_identifier = item["Track Identifier"]

        if s.exec(
            select(AppleMediaServicesTrack).where(
                AppleMediaServicesTrack.track_identifier == track_identifier
            )
        ).one_or_none():
            if progress is not None:
                progress.advance(task_id)
            continue

        duration_ms = item.get("Track Duration")
        track_defaults = dict(
            track_n=item.get("Track Number On Album"),
            disc_n=item.get("Disc Number Of Album"),
            compilation=item.get("Is Part of Compilation"),
            year=item.get("Track Year"),
            duration=round(duration_ms / 1000) if duration_ms else None,
            date_added=_parse_dt(item.get("Date Added To Library")),
        )
        track, track_created = Track.get_or_create(
            s,
            title=item["Title"],
            artist=item.get("Artist"),
            album=item.get("Album"),
            album_artist=item.get("Album Artist"),
            defaults=track_defaults,
        )
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        ms_track = AppleMediaServicesTrack(
            track=track,
            track_identifier=track_identifier,
            title=item["Title"],
            artist=item.get("Artist"),
            album=item.get("Album"),
            album_artist=item.get("Album Artist"),
            compilation=item.get("Is Part of Compilation", False),
            track_number=item.get("Track Number On Album"),
            disc_number=item.get("Disc Number Of Album"),
            track_count=item.get("Track Count On Album"),
            disc_count=item.get("Disc Count Of Album"),
            year=item.get("Track Year"),
            duration=duration_ms,
            play_count=item.get("Track Play Count"),
            skip_count=item.get("Skip Count"),
            date_added=_parse_dt(item.get("Date Added To Library")),
            date_modified=_parse_dt(item.get("Last Modified Date")),
            release_date=_parse_dt(item.get("Release Date")),
            genre=item.get("Genre"),
            audio_file_extension=item.get("Audio File Extension") or None,
            is_purchased=bool(item.get("Is Purchased", False)),
            purchased_track_identifier=item.get("Purchased Track Identifier") or None,
            audio_matched_track_identifier=item.get("Audio Matched Track Identifier") or None,
        )
        s.add(ms_track)
        s.flush()

        for tp in _get_track_full_paths(ms_track, root_dir):
            tf, _ = TrackFile.get_or_create(s, source_path=tp, defaults=dict(track_id=track.id))
            tf.enrich()
        s.flush()

        if progress is not None:
            progress.update(task_id, advance=1, created=created, updated=updated)

    return created, updated


def do_import_ms_playlists(
    s: Session,
    playlists: list[dict],
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> int:
    """Import Apple Media Services playlists. Returns number of new playlist rows created."""
    created = 0
    for pl in playlists:
        container_type = pl.get("Container Type", "")
        title = pl.get("Title", "")

        if progress is not None:
            progress.update(task_id, description=title)

        if container_type in _SKIP_PLAYLIST_TYPES:
            if progress is not None:
                progress.advance(task_id)
            continue

        item_identifiers = pl.get("Playlist Item Identifiers") or []
        if not item_identifiers:
            if progress is not None:
                progress.advance(task_id)
            continue

        container_id = pl["Container Identifier"]
        pl_db = s.exec(
            select(AppleMediaServicesPlaylist).where(
                AppleMediaServicesPlaylist.container_identifier == container_id
            )
        ).one_or_none()

        if not pl_db:
            pl_db = AppleMediaServicesPlaylist(
                container_identifier=container_id,
                title=title,
                container_type=container_type,
                parent_folder_identifier=pl.get("Parent Folder Identifier"),
                date_added=_parse_dt(pl.get("Added Date")),
            )
            s.add(pl_db)
            s.flush()
            created += 1

        existing_track_ids = {
            link.track_id
            for link in s.exec(
                select(AppleMediaServicesPlaylistTrack).where(
                    AppleMediaServicesPlaylistTrack.playlist_id == pl_db.id
                )
            )
        }

        ms_tracks_by_identifier = {
            t.track_identifier: t
            for t in s.exec(
                select(AppleMediaServicesTrack).where(
                    AppleMediaServicesTrack.track_identifier.in_(item_identifiers)
                )
            )
        }

        seen = set()
        pos = 0
        for track_identifier in item_identifiers:
            if track_identifier in seen:
                continue
            ms_track = ms_tracks_by_identifier.get(track_identifier)
            if ms_track is None:
                continue
            seen.add(track_identifier)
            pos += 1
            if ms_track.id not in existing_track_ids:
                link = AppleMediaServicesPlaylistTrack(track=ms_track, playlist=pl_db, position=pos)
                s.add(link)

        s.flush()

        if progress is not None:
            progress.advance(task_id)

    return created


def import_apple_media_services(activity_dir: str, root_dir: str, reset: bool = False):
    activity_path = Path(activity_dir)
    root_path = Path(root_dir)

    if reset:
        with Session(engine) as s:
            s.exec(delete(AppleMediaServicesPlaylist))
            s.exec(delete(AppleMediaServicesTrack))
            s.commit()
        console.print("[yellow]Apple Media Services data purged[/yellow]")

    tracks_zip = activity_path / "Apple Music Library Tracks.json.zip"
    playlists_zip = activity_path / "Apple Music Library Playlists.json.zip"

    tracks_data = _load_json_zip(tracks_zip)
    playlists_data = _load_json_zip(playlists_zip)

    with Session(engine) as s:
        with make_import_progress() as progress:
            task = progress.add_task("Tracks", total=len(tracks_data), created=0, updated=0)
            created, updated = do_import_ms_tracks(s, tracks_data, root_path, progress=progress, task_id=task)
        console.print(f"Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]")

        with make_progress() as progress:
            task = progress.add_task("Playlists", total=len(playlists_data))
            n_playlists = do_import_ms_playlists(s, playlists_data, progress=progress, task_id=task)
        console.print(f"Playlists: [green]{n_playlists} new[/green]")

        s.commit()

    console.print("[green]Apple Media Services import finished[/green]")
