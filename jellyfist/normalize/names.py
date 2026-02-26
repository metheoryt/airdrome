from sqlmodel import Session, select

from jellyfist.models import Track, engine, TrackAlias
from .norm import normalize_name


def normalize_track_names():
    with Session(engine) as s:
        for track in s.exec(select(Track)):
            track: Track
            track.name_norm = normalize_name(track.name)
            track.album_norm = normalize_name(track.album)
            track.artist_norm = normalize_name(track.artist)
            track.album_artist_norm = normalize_name(track.album_artist)
    print("track names normalized")


def normalize_alias_names():
    with Session(engine) as s:
        for track_alias in s.exec(select(TrackAlias)):
            track_alias: TrackAlias
            track_alias.title_norm = normalize_name(track_alias.title)
            track_alias.album_norm = normalize_name(track_alias.album)
            track_alias.artist_norm = normalize_name(track_alias.artist)
    print("track alias names normalized")
