import json
from pathlib import Path

from pydantic import BaseModel


class DupGroup(BaseModel):
    members: list[int]
    canons: list[int | None]

    @classmethod
    def dump(cls, data: dict[str, "DupGroup"], path: Path):
        if not data:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: v.model_dump(mode="json") for k, v in data.items()}, f)

    @classmethod
    def load(cls, path: Path) -> dict[str, "DupGroup"]:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return {k: cls(**v) for k, v in obj.items()}
