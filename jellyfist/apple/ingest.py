import json
import plistlib
from pathlib import Path

from sqlmodel import Session, select, SQLModel

from jellyfist.models import Track, engine, Playlist, TrackPlaylistLink, TrackFile
from .schemas import TrackSchema, PlaylistSchema

LIBRARY_PATH = Path(r"C:\Users\methe\Music\iTunes\iTunes Media\Music")


def get_track_paths(t: TrackSchema) -> set[Path]:
    paths = set()

    if t.apple_music:
        # cloud track doesn't have a file on disk
        return paths

    for track_path in set(t.possible_locations()):
        full_path = LIBRARY_PATH / track_path

        if full_path.exists():
            paths.add(track_path)

    return paths


DUPLICATE_TRACKS = {}


def maybe_load_duplicates(filename: Path):
    # load duplicates from a file
    if not filename.exists():
        return
    with open(filename, "r") as f:
        for k, v in json.load(f).items():
            DUPLICATE_TRACKS[k] = v
    print(len(DUPLICATE_TRACKS), "duplicate rules loaded")


def dump_duplicates(filename: Path):
    if not DUPLICATE_TRACKS:
        return
    with open(filename, "w") as f:
        json.dump(DUPLICATE_TRACKS, f, indent=2)
        print(len(DUPLICATE_TRACKS), "duplicate rules saved")


def ingest_library(filename: str, recreate: bool = False, reset_duplicates: bool = False):
    if recreate:
        print("recreating db")
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine, checkfirst=True)

    with open(filename, "rb") as f:
        plist = plistlib.load(f)

    # load duplicates from a file
    filedir = Path(filename).parent
    duplicates_filename = filedir / "duplicates.json"
    if reset_duplicates:
        duplicates_filename.unlink(missing_ok=True)
    maybe_load_duplicates(duplicates_filename)

    with Session(engine) as session:
        print("ingesting tracks")
        for track_id, data in plist["Tracks"].items():
            track_schema = TrackSchema(**data)

            # check if the track already exists
            existing_track: Track = session.exec(
                select(Track).where(
                    Track.name_norm == track_schema.name_norm,
                    Track.album_norm == track_schema.album_norm,
                    # Track.artist == track_schema.artist,
                    # Track.track_number == track_schema.track_number,
                )
            ).one_or_none()
            if existing_track:
                print("duplicate track found:")
                print("Existing:", existing_track.short_info)
                print("Incoming:", track_schema.short_info)
                prompt = input("Leave [e]xisting, replace with [i]ncoming, or [K]eep both? e/i/K:")
                if prompt.lower() in ["k", ""]:
                    # not duplicates
                    pass
                elif prompt.lower() == "i":
                    # Delete the existing track to replace it with incoming.
                    # update some data though
                    track_schema.date_added = min(existing_track.date_added, track_schema.date_added)

                    DUPLICATE_TRACKS[existing_track.track_id] = track_schema.track_id
                    session.delete(existing_track)
                    session.flush()
                elif prompt.lower() == "e":
                    # discard the incoming track.
                    # update some data though
                    if track_schema.date_added > existing_track.date_added:
                        existing_track.date_added = track_schema.date_added
                        session.flush()

                    DUPLICATE_TRACKS[track_schema.track_id] = existing_track.track_id
                    continue
                else:
                    raise ValueError(f"Invalid prompt: {prompt}")

            track_data = {k: getattr(track_schema, k) for k in TrackSchema.model_fields}
            track = Track(**track_data)
            session.add(track)
            session.flush()

            track_paths = get_track_paths(track_schema)
            for tp in track_paths:
                tf = TrackFile(track_id=track.id, path=str(tp))
                session.add(tf)
                session.flush()

        session.commit()

        dump_duplicates(duplicates_filename)

        print("ingesting playlists")
        for pl in plist["Playlists"]:
            pls = PlaylistSchema(**pl)
            if pls.smart_info:
                print("skipping smart playlist", pls.name)
                continue

            print("ingesting", pls.name)
            playlist = Playlist(
                name=pls.name,
                description=pls.description,
                playlist_id=pls.playlist_id,
                all_items=pls.all_items,
                persistent_id=pls.persistent_id,
                parent_persistent_id=pls.parent_persistent_id,
                master=pls.master,
                visible=pls.visible,
                music=pls.music,
                folder=pls.folder,
                distinguished_kind=pls.distinguished_kind,
                favorited=pls.favorited,
                loved=pls.loved,
            )
            session.add(playlist)
            session.commit()

            for pls_track in pls.items:
                if pls_track.track_id in DUPLICATE_TRACKS:
                    # if the track was replaced, use the replacement
                    track_id = DUPLICATE_TRACKS[pls_track.track_id]
                else:
                    track_id = pls_track.track_id

                # one track has to be there
                track = session.exec(select(Track).where(Track.track_id == track_id)).one()

                tpl = TrackPlaylistLink(
                    track_id=track.id,
                    playlist_id=playlist.id,
                )
                session.add(tpl)
                session.flush()
            session.commit()
    print("done")
