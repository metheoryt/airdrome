import plistlib
from pathlib import Path

from sqlmodel import Session, select, SQLModel

from jellyfist.models import Track, engine, Playlist, TrackPlaylistLink, TrackFile
from .schemas import TrackSchema, PlaylistSchema
from jellyfist.conf import settings


def get_track_paths(t: TrackSchema) -> set[Path]:
    paths = set()

    if t.apple_music:
        # cloud track doesn't have a file on disk
        return paths

    for track_path in set(t.possible_locations()):
        full_path = settings.apple_music_library_dirpath / track_path

        if full_path.exists():
            paths.add(track_path)

    return paths


def ingest_library(filename: str, recreate: bool = False):
    if recreate:
        print("recreating db")
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine, checkfirst=True)

    with open(filename, "rb") as f:
        plist = plistlib.load(f)

    with Session(engine) as session:
        print("ingesting tracks")
        for track_id, data in plist["Tracks"].items():
            track_schema = TrackSchema(**data)

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

            already_added = set()  # to exclude duplicate records from the playlist
            for pls_track in pls.items:
                if pls_track.track_id in already_added:
                    continue

                track = session.exec(select(Track).where(Track.track_id == pls_track.track_id)).one()

                tpl = TrackPlaylistLink(
                    track_id=track.id,
                    playlist_id=playlist.id,
                )
                session.add(tpl)
                already_added.add(pls_track.track_id)

            session.commit()
    print("done")
