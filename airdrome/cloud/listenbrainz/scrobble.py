import io
import zipfile
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


def _parse_jsonl_lines(lines: Iterator[str]) -> Iterator[ListenBrainzScrobble]:
    for line in lines:
        line = line.strip()
        if line:
            yield ListenBrainzScrobble.model_validate_json(line)


def get_lb_records(path: Path) -> Iterator[ListenBrainzScrobble]:
    if path.is_file():
        with zipfile.ZipFile(path) as z:
            for entry in sorted(z.namelist()):
                if not entry.startswith("listens/") or not entry.endswith(".jsonl"):
                    continue
                with z.open(entry) as f:
                    yield from _parse_jsonl_lines(io.TextIOWrapper(f, encoding="utf-8"))
    else:
        listens_dir = path / "listens" if (path / "listens").is_dir() else path
        for jsonl_file in sorted(listens_dir.rglob("*.jsonl")):
            with open(jsonl_file, encoding="utf-8") as f:
                yield from _parse_jsonl_lines(f)


class ListenBrainzScrobbleParser(ScrobbleParser):
    platform = Platform.LISTENBRAINZ

    def __init__(self, path: Path):
        self.path = path

    def _iterate_scrobbles(self) -> Iterator[tuple[TrackAlias, datetime]]:
        for record in get_lb_records(self.path):
            yield (
                TrackAlias(artist=record.artist_name, album=record.release_name, title=record.track_name),
                record.listened_at,
            )
