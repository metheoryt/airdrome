from datetime import datetime
from pathlib import Path

from sqlmodel import Session, delete, select

from airdrome.console import console, make_import_progress, make_progress

from .models import AppleMSPlaylist, AppleMSPlaylistTrack, AppleMSTrack
from .package import AppleMediaServicesPackage


_SKIP_PLAYLIST_TYPES = {"Smart Playlist", "Genius Mix", "Genius Playlist", "Folder"}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def import_ms_track(s: Session, item: dict) -> bool:
    """Import Apple Media Services tracks. Yields whether an AppleMSTrack record was created."""
    track_identifier = item["Track Identifier"]

    if s.exec(select(AppleMSTrack).where(AppleMSTrack.track_identifier == track_identifier)).one_or_none():
        return False

    ms_track = AppleMSTrack(
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
        duration=item.get("Track Duration"),
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
    return True


def import_ms_playlist(s: Session, pl: dict) -> bool:
    """Import Apple Media Services playlists. Returns the number of new playlist rows created."""
    container_id = pl["Container Identifier"]
    container_type = pl.get("Container Type", "")
    title = pl.get("Title", "")
    created = False

    if container_type in _SKIP_PLAYLIST_TYPES:
        return created

    item_identifiers = pl.get("Playlist Item Identifiers") or []
    if not item_identifiers:
        return created

    pl_db = s.exec(
        select(AppleMSPlaylist).where(AppleMSPlaylist.container_identifier == container_id)
    ).one_or_none()

    if not pl_db:
        pl_db = AppleMSPlaylist(
            container_identifier=container_id,
            title=title,
            container_type=container_type,
            parent_folder_identifier=pl.get("Parent Folder Identifier"),
            date_added=_parse_dt(pl.get("Added Date")),
            items_modified_date=_parse_dt(pl.get("Playlist Items Modified Date")),
        )
        s.add(pl_db)
        s.flush()
        created = True

    if not created:
        # clear all playlist members to insert again
        s.exec(delete(AppleMSPlaylistTrack).where(AppleMSPlaylistTrack.playlist_id == pl_db.id))

    ms_tracks = s.exec(select(AppleMSTrack).where(AppleMSTrack.track_identifier.in_(item_identifiers)))
    ms_tracks_by_identifier = {t.track_identifier: t for t in ms_tracks}

    pos = 0
    for track_identifier in item_identifiers:
        if ms_track := ms_tracks_by_identifier.get(track_identifier) is None:
            # the track referenced in the playlist is not in the library, skip it
            continue

        pos += 1
        s.add(AppleMSPlaylistTrack(track=ms_track, playlist=pl_db, position=pos))

    s.flush()

    return created


def import_apple_media_services(s: Session, path: str, reset: bool = False):
    package = AppleMediaServicesPackage(Path(path))
    track_items = package.load_tracks()
    playlist_items = package.load_playlists()

    if reset:
        s.exec(delete(AppleMSPlaylist))
        s.exec(delete(AppleMSTrack))
        s.flush()
        console.print("[yellow]Apple Media Services data purged[/yellow]")

    with make_import_progress() as progress:
        task = progress.add_task("Tracks", total=len(track_items), created=0)
        created_cnt = 0

        for item in track_items:
            created = import_ms_track(s, item)
            if not created:
                progress.advance(task)
                continue

            created_cnt += 1
            if created_cnt % 100 == 0:
                s.flush()
            progress.update(task, advance=1, created=created_cnt)

    console.print(f"Tracks: [green]{created_cnt} new[/green]")

    with make_progress() as progress:
        created_cnt = 0
        task = progress.add_task("Playlists", total=len(playlist_items))
        for pl in playlist_items:
            progress.update(task, description=pl.get("Title", ""))
            created = import_ms_playlist(s, pl)
            progress.advance(task)
            if created:
                created_cnt += 1
    console.print(f"Playlists: [green]{created_cnt} new[/green]")

    console.print("[green]Apple Media Services import finished[/green]")
