import csv
from datetime import datetime
from typing import Iterator

from pydantic import BaseModel, Field, model_validator

from airdrome.enums import Platform
from airdrome.models import TrackAlias
from airdrome.scrobbles.parser import ScrobbleParser


class AppleMusicPlayActivity(BaseModel):
    # Apple play activity doesn't contain an artist name,
    # so we will match by album/track names only

    album_name: str | None = Field(None, alias="Album Name")
    song_name: str | None = Field(None, alias="Song Name")
    play_duration_ms: int = Field(alias="Play Duration Milliseconds")
    event_ts: datetime = Field(alias="Event Timestamp")

    @model_validator(mode="before")
    @classmethod
    def cast_data(cls, data: dict) -> dict:
        for k in data:
            if not data[k]:
                data[k] = None

        if not data.get("Event Timestamp"):
            # sometimes there's no event timestamp, we use the end timestamp instead, they're similar
            data["Event Timestamp"] = data["Event End Timestamp"]
        return data


def get_scrobbles(play_activity_csv_path: str):
    with open(play_activity_csv_path, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row["Event Type"] != "PLAY_END":
                continue

            r = AppleMusicPlayActivity(**row)
            if r.play_duration_ms < 30_000:
                continue
            if not r.song_name:
                continue

            yield r


class AppleScrobbleParser(ScrobbleParser):
    platform = Platform.APPLE

    def __init__(self, play_activity_csv_path: str):
        self.play_activity_csv_path = play_activity_csv_path

    def _iterate_scrobbles(self) -> Iterator[tuple[TrackAlias, datetime]]:
        for r in get_scrobbles(self.play_activity_csv_path):
            yield TrackAlias(album=r.album_name, title=r.song_name), r.event_ts
