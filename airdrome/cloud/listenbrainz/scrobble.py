from datetime import datetime
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, model_validator

from airdrome.enums import Platform
from airdrome.models import TrackAlias
from airdrome.scrobbles.parser import ScrobbleParser


class ListenBrainzScrobble(BaseModel):
    track_name: str
    artist_name: str
    release_name: str | None = None
    listened_at: datetime

    @model_validator(mode="before")
    @classmethod
    def extract_data(cls, data: dict) -> dict:
        meta = data["track_metadata"]
        return dict(
            track_name=meta["track_name"],
            artist_name=meta["artist_name"],
            release_name=meta.get("release_name"),
            listened_at=data["listened_at"],
        )


def get_lb_records(listens_dir_path: Path) -> Iterator[ListenBrainzScrobble]:
    for listens_jsonl in listens_dir_path.rglob("*.jsonl"):
        with open(listens_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                yield ListenBrainzScrobble.model_validate_json(line)


class ListenBrainzScrobbleParser(ScrobbleParser):
    platform = Platform.LISTENBRAINZ

    def __init__(self, listens_dir_path: Path):
        self.listens_dir_path = listens_dir_path

    def _iterate_scrobbles(self) -> Iterator[tuple[TrackAlias, datetime]]:
        for record in get_lb_records(self.listens_dir_path):
            yield (
                TrackAlias(artist=record.artist_name, album=record.release_name, title=record.track_name),
                record.listened_at,
            )
