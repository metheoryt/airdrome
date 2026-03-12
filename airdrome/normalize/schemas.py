import json
import os

from pydantic import BaseModel


class DupGroup(BaseModel):
    members: list[int]
    canons: list[int | None]

    @classmethod
    def dump(cls, data: dict[str, "DupGroup"], path: str):
        if not data:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: v.model_dump(mode="json") for k, v in data.items()}, f)

    @classmethod
    def load(cls, path: str) -> dict[str, "DupGroup"]:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return {k: cls(**v) for k, v in obj.items()}
