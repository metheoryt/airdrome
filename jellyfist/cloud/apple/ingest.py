import plistlib
from pathlib import Path

from sqlmodel import Session, select, delete, exists

from jellyfist.conf import settings
from jellyfist.models import Track, TrackFile, engine
from .models import AppleTrack, ApplePlaylistImport, ApplePlaylist, ApplePlaylistTrack


def get_track_paths(t: AppleTrack) -> set[Path]:
    paths = set()

    # if t.apple_music:
    #     # cloud track doesn't have a file on disk
    #     return paths

    for track_path in t.possible_locations(max_suffix=2):
        full_path = settings.apple_music_library_dirpath / track_path

        if full_path.exists():
            paths.add(track_path)

    return paths


def import_apple_library(filename: str, reset: bool = False):
    if reset:
        print("Purging imported Apple library...")
        with Session(engine) as s:
            s.exec(delete(Track).where(exists().where(AppleTrack.track_id == Track.id)))
            s.exec(delete(ApplePlaylist))
            s.commit()
        print("Apple library purged")
        input("hold on")

    with open(filename, "rb") as f:
        plist = plistlib.load(f)

    with Session(engine) as s:
        for i, (track_id, data) in enumerate(plist["Tracks"].items()):
            apple_track_schema = AppleTrack(**data)

            apple_track = s.exec(
                select(AppleTrack).where(AppleTrack.apple_track_id == apple_track_schema.apple_track_id)
            ).one_or_none()
            if apple_track:
                # already exists
                print("apple track already exists:", apple_track_schema.apple_track_id, " - skipping")
                continue

            # track_data = {k: getattr(track_schema, k) for k in TrackSchema.model_fields()}
            apple_track = apple_track_schema

            # bind to an Airdrome Track (many to 1)
            track, created = Track.get_or_create(
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
            # create apple track already bound to a track
            apple_track.track = track
            s.add(apple_track)
            s.flush()

            for tp in get_track_paths(apple_track):
                TrackFile.get_or_create(s, track=track, path=str(tp))
            print(f"Imported {i + 1:>8} of {len(plist['Tracks'])} tracks", end="\r", flush=True)

        print()
        print(f"Importing {len(plist['Playlists'])} playlists")
        for pl in plist["Playlists"]:
            pl_import = ApplePlaylistImport(**pl)
            if pl_import.smart_info:
                print("Skipping smart playlist", pl_import.name)
                continue

            pl_db = s.exec(
                select(ApplePlaylist).where(ApplePlaylist.playlist_id == pl_import.playlist_id)
            ).one_or_none()
            if not pl_db:
                pl_db = ApplePlaylist.model_validate(pl_import)
                s.add(pl_db)
                s.flush()

            seen = set()  # to exclude duplicate records from the playlist
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
                apt = ApplePlaylistTrack(
                    track=pl_tracks[pls_track.apple_track_id], playlist=pl_db, position=pos
                )
                s.add(apt)
                seen.add(pls_track.apple_track_id)
            s.flush()
            print(f"Imported {len(seen):>7} tracks into", pl_import.name)

        # commit at the end
        s.commit()
    print("Apple library import finished")
