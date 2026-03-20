from sqlmodel import Session, select

from airdrome.models import Track, TrackAlias, TrackFile

from .norm import normalize_name


def normalize_track_names(s: Session):
    i = 0
    for track in s.exec(select(Track)):
        track: Track
        track.title_norm = normalize_name(track.title)
        track.album_norm = normalize_name(track.album)
        track.artist_norm = normalize_name(track.artist)
        track.album_artist_norm = normalize_name(track.album_artist)
        i += 1
        if i % 1000 == 0:
            s.flush()
    s.flush()
    print("track names normalized")


def normalize_alias_names(s: Session):
    i = 0
    for track_alias in s.exec(select(TrackAlias)):
        track_alias: TrackAlias
        track_alias.title_norm = normalize_name(track_alias.title)
        track_alias.album_norm = normalize_name(track_alias.album)
        track_alias.artist_norm = normalize_name(track_alias.artist)
        i += 1
        if i % 1000 == 0:
            s.flush()
    s.flush()
    print("track alias names normalized")


def normalize_track_file_names(s: Session):
    i = 0
    for track_file in s.exec(select(TrackFile)):
        track_file: TrackFile
        track_file.title_norm = normalize_name(track_file.title)
        track_file.album_norm = normalize_name(track_file.album)
        track_file.artist_norm = normalize_name(track_file.artist)
        track_file.album_artist_norm = normalize_name(track_file.album_artist)
        i += 1
        if i % 1000 == 0:
            s.flush()
    s.flush()
    print("track file names normalized")
