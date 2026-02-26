import csv
from datetime import datetime, date, timedelta
from typing import Iterator

from jellyfist.enums import Platform
from jellyfist.scrobbles.parser import ScrobbleParser
from jellyfist.scrobbles.schemas import TrackScrobble, TrackAliasSchema
from .schemas import LastFMScrobble


def get_lastfm_records(filepath: str) -> Iterator["LastFMScrobble"]:
    missing_date = datetime(2010, 1, 1)
    with open(filepath, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        for row in reader:
            r = LastFMScrobble(artist=row[0], album=row[1], title=row[2], date=row[3])
            if not r.title:
                continue

            if r.date.date() == date(1970, 1, 1):
                # fix the missing date.
                # make is start from 2010, increment by 5 minutes
                r.date = missing_date
                missing_date += timedelta(minutes=5)
            yield r


class LastFMScrobbleParser(ScrobbleParser):
    platform = Platform.LASTFM

    def __init__(self, filepath: str):
        self.filepath = filepath

    def _iterate_scrobbles(self) -> Iterator[TrackScrobble]:
        for record in get_lastfm_records(self.filepath):
            yield TrackScrobble(
                alias=TrackAliasSchema(artist=record.artist, album=record.album, title=record.title),
                date=record.date,
            )
