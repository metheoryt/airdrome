import plistlib
from pathlib import Path

from sqlmodel import Session, delete, exists, select

from airdrome.console import console
from airdrome.models import Track, TrackFile, engine

from .models import ApplePlaylist, ApplePlaylistImport, ApplePlaylistTrack, AppleTrack


def get_track_full_paths(t: AppleTrack, root_dir: str) -> set[Path]:
    paths = set()

    for track_path in t.possible_locations(max_suffix=2):
        full_path = root_dir / track_path

        if full_path.exists():
            paths.add(full_path.resolve())

    return paths


def do_import_tracks(s: Session, tracks_data: dict, root_dir: Path) -> int:
    """
    Import Apple Music tracks into the database. Returns number of new tracks created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for track_id, data in tracks_data.items():
        at = AppleTrack(**data)

        apple_track = s.exec(
            select(AppleTrack).where(AppleTrack.apple_track_id == at.apple_track_id)
        ).one_or_none()
        if apple_track:
            continue

        apple_track = at

        track, _ = Track.get_or_create(
            s,
            title=apple_track.name,
            artist=apple_track.artist,
            album=apple_track.album,
            album_artist=apple_track.album_artist,
            defaults=dict(
                track_n=apple_track.track_number,
                disc_n=apple_track.disc_number,
                compilation=apple_track.compilation,
            ),
        )
        apple_track.track = track
        s.add(apple_track)
        s.flush()
        created += 1

        if not apple_track.apple_music:
            for tp in get_track_full_paths(apple_track, root_dir):
                tf, _ = TrackFile.get_or_create(s, track_id=track.id, source_path=tp)
                tf.enrich()
        s.flush()

    return created


def do_import_playlists(s: Session, playlists_data: list) -> int:
    """
    Import Apple Music playlists into the database. Returns number of new playlists created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for pl in playlists_data:
        pl_import = ApplePlaylistImport(**pl)
        if pl_import.smart_info:
            console.print(f"[dim]skipping smart playlist: {pl_import.name}[/dim]")
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
        console.print(f"  [cyan]{len(seen):>7}[/cyan]  {pl_import.name}")

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
        console.print(f"Importing [bold]{len(plist['Tracks'])}[/bold] tracks")
        do_import_tracks(s, plist["Tracks"], root_dir)

        console.print(f"Importing [bold]{len(plist['Playlists'])}[/bold] playlists")
        do_import_playlists(s, plist["Playlists"])

        s.commit()

    console.print("[green]Apple library import finished[/green]")
