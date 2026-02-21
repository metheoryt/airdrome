import csv
from datetime import datetime

from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, select

from jellyfist.models import Track, engine


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


def get_apple_records(play_activity_csv_path: str):
    with open(play_activity_csv_path, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row["Event Type"] != "PLAY_END":
                continue

            r = AppleMusicPlayActivity(**row)
            if r.play_duration_ms < 25_000:
                continue
            if not r.song_name and not r.album_name:
                continue
            if not r.song_name or not r.album_name:
                # print(r)
                continue

            yield r


def ingest_play_history(play_activity_csv_path: str):
    mismatch = set()
    duplicate = set()
    with Session(engine) as session:
        for record in get_apple_records(play_activity_csv_path):
            tracks = session.exec(
                select(Track).where(Track.name == record.song_name, Track.album == record.album_name)
            ).all()
            if not tracks:
                mismatch.add((record.album_name, record.song_name))
            elif len(tracks) > 1:
                duplicate.add((record.album_name, record.song_name))
    return mismatch, duplicate
