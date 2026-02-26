import json
import os
from typing import Iterator

from jellyfist.enums import Platform
from jellyfist.scrobbles.parser import ScrobbleParser
from jellyfist.scrobbles.schemas import TrackScrobble, TrackAliasSchema


def get_spotify_scrobbles(filename: str):
    with open(filename, mode="r", encoding="utf-8") as jsonfile:
        history = json.load(jsonfile)
        for record in history:
            if record["ms_played"] < 30000:
                # do not import <30s plays
                continue

            artist, album, title = (
                record["master_metadata_album_artist_name"],
                record["master_metadata_album_album_name"],
                record["master_metadata_track_name"],
            )
            if not title:
                continue

            yield TrackScrobble(
                alias=TrackAliasSchema(artist=artist, album=album, title=title), date=record["ts"]
            )


def get_spotify_streaming_history(dirpath: str):
    for filename in os.listdir(dirpath):
        yield from get_spotify_scrobbles(os.path.join(dirpath, filename))


class SpotifyScrobbleParser(ScrobbleParser):
    platform = Platform.SPOTIFY

    def __init__(self, dirpath: str):
        self.dirpath = dirpath

    def _iterate_scrobbles(self) -> Iterator[TrackScrobble]:
        for r in get_spotify_streaming_history(self.dirpath):
            yield r
