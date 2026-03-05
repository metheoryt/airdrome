from pydantic import BaseModel
import json
import os


class DupGroup(BaseModel):
    canon_id: int
    twin_ids: list[int]

    @classmethod
    def dump(cls, data: dict[str, list["DupGroup"]], path: str):
        if not data:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: [v.model_dump(mode="json") for v in group] for k, group in data.items()}, f)

    @classmethod
    def load(cls, path: str) -> dict[str, list["DupGroup"]]:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return {k: [cls(**v) for v in group] for k, group in obj.items()}
