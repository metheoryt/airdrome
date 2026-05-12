import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict


FlagMap = dict[str, bool]


AUTO_DEDUP_FLAG_KEYS: frozenset[str] = frozenset(
    {
        "with_artist",
        "with_album_artist",
        "with_album",
        "with_track_n",
        "with_disc_n",
        "with_duration",
        "with_year",
    }
)


class RunRecord(TypedDict):
    ran_at: str
    flags: FlagMap
    groups: int
    twins: int


def load_history(path: Path) -> list[RunRecord]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError, json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def record_run(path: Path, flags: FlagMap, groups: int, twins: int) -> bool:
    """Append a run to the history. Returns True if appended, False if skipped.

    Skipped when the run produced no groups, or the same flag-set is already
    present in history.
    """
    if groups == 0:
        return False
    history = load_history(path)
    if any(entry.get("flags") == flags for entry in history):
        return False
    history.append(
        RunRecord(
            ran_at=datetime.now(timezone.utc).isoformat(),
            flags=flags,
            groups=groups,
            twins=twins,
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    return True


def format_flags(flags: FlagMap) -> str:
    """Render a flag-map as the CLI args that would reproduce it (defaults = all on)."""
    off = [name.removeprefix("with_") for name, on in flags.items() if not on]
    return " ".join(f"--no-{name.replace('_', '-')}" for name in off) if off else "(defaults)"
