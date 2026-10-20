"""Pair decisions keyed by track natural key, and the pure newest-wins resolver.

A decision names tracks by `(title, artist, album, album_artist)` — the raw columns of the `Track`
unique constraint — because ids do not survive a DB rebuild. `resolve` turns decisions plus the live
key -> track-id map into must-link components, cannot-links and canon picks, independent of input order.
This module is pure: no session, no ORM at runtime.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from airdrome.enums import DecisionKind, DecisionOrigin


if TYPE_CHECKING:
    from airdrome.models import Track


type Key = tuple[str, str | None, str | None, str | None]


def track_key(t: Track) -> Key:
    """Return the natural key of a track from its raw (not `_norm`) columns."""
    return (t.title, t.artist, t.album, t.album_artist)


def key_text(k: Key) -> str:
    """Serialize a key to its one stored and compared form (compact JSON; None stays apart from "")."""
    return json.dumps(list(k), ensure_ascii=False, separators=(",", ":"))


def parse_key(text: str | list) -> Key:
    """Parse a key from its text form or the mirror's list form; ValueError unless title + 3 str|None."""
    value = json.loads(text) if isinstance(text, str) else text
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"a key is a list of 4 items, got {value!r}")
    title, artist, album, album_artist = value
    if not isinstance(title, str) or any(v is not None and not isinstance(v, str) for v in value[1:]):
        raise ValueError(f"a key is a str title and three str | None, got {value!r}")
    return (title, artist, album, album_artist)


def require_aware(decided_at: datetime) -> None:
    """Raise ValueError for a naive datetime (all decision times are tz-aware UTC)."""
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ValueError("decided_at must be timezone-aware")


@dataclass(frozen=True)
class Pair:
    """A same/different decision about two track keys (as key text), stored with a < b."""

    a: str
    b: str
    kind: DecisionKind
    decided_at: datetime
    origin: DecisionOrigin


def pair(x: str, y: str, kind: DecisionKind, decided_at: datetime, origin: DecisionOrigin) -> Pair:
    """Build a Pair with ordered ends; ValueError on identical ends or a naive `decided_at`."""
    if x == y:
        raise ValueError("a pair decision needs two different tracks")
    require_aware(decided_at)
    a, b = sorted((x, y))
    return Pair(a, b, kind, decided_at, origin)


@dataclass(frozen=True)
class Canon:
    """A choice of one track key (as text) as the main version of whatever group it ends up in."""

    track: str
    decided_at: datetime
    origin: DecisionOrigin


class Outcome(StrEnum):
    """What the resolver did with a pair decision."""

    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    DEAD = "dead"


@dataclass(frozen=True)
class Resolution:
    """The resolver's output: components, cannot-links, canon picks and per-pair outcomes."""

    components: list[frozenset[int]]
    cannot_link: frozenset[frozenset[int]]
    canons: dict[int, datetime]
    outcomes: dict[tuple[str, str], Outcome]
    dead_canons: int


class _Walk:
    """Union-find over track ids where each root also holds the set of roots it is separated from."""

    def __init__(self) -> None:
        """Start with no tracks."""
        self.parent: dict[int, int] = {}
        self.sep: dict[int, set[int]] = {}

    def find(self, x: int) -> int:
        """Return the root of `x`, registering it on first sight."""
        if x not in self.parent:
            self.parent[x] = x
            self.sep[x] = set()
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def same(self, x: int, y: int) -> bool:
        """Union the components of x and y; False (nothing changed) if they are separated."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return True
        if ry in self.sep[rx]:
            return False
        self.parent[ry] = rx
        # Separations stay transitive: rx inherits ry's, and everyone who named ry now names rx.
        for s in self.sep.pop(ry):
            self.sep[s].discard(ry)
            self.sep[s].add(rx)
            self.sep[rx].add(s)
        return True

    def different(self, x: int, y: int) -> bool:
        """Separate the components of x and y; False (nothing changed) if they are already one."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        self.sep[rx].add(ry)
        self.sep[ry].add(rx)
        return True


def resolve(pairs: Iterable[Pair], canons: Iterable[Canon], live: Mapping[str, int]) -> Resolution:
    """Resolve decisions into components, cannot-links and canons; `live` maps key text -> id.

    Pairs are applied in this order: `decided_at` newest first; at equal `decided_at`, every `same`
    before any `different`; any remaining tie by (a, b) key order. A pair that contradicts what was
    already applied is SUPERSEDED. The order is total, so the result never depends on input order.
    """
    outcomes: dict[tuple[str, str], Outcome] = {}
    walk = _Walk()
    # Decisions sharing one timestamp come from one old group, where `same` pairs are explicit choices
    # and `different` pairs are only implied by not being in the group. They conflict only when a newer
    # decision already broke that old picture, and then the join should survive: same goes first.
    ordered = sorted(
        pairs,
        key=lambda p: (-p.decided_at.timestamp(), p.kind != DecisionKind.SAME, p.a, p.b),
    )
    for p in ordered:
        if p.a not in live or p.b not in live:
            outcomes[(p.a, p.b)] = Outcome.DEAD
            continue
        x, y = live[p.a], live[p.b]
        ok = walk.same(x, y) if p.kind == DecisionKind.SAME else walk.different(x, y)
        outcomes[(p.a, p.b)] = Outcome.ACCEPTED if ok else Outcome.SUPERSEDED

    members: dict[int, set[int]] = {}
    for track_id in walk.parent:
        members.setdefault(walk.find(track_id), set()).add(track_id)
    components = sorted((frozenset(m) for m in members.values() if len(m) >= 2), key=min)
    cannot_link = frozenset(
        frozenset((i, j))
        for r, others in walk.sep.items()
        for o in others
        for i in members[r]
        for j in members[o]
    )

    live_canons: dict[int, datetime] = {}
    dead_canons = 0
    for c in canons:
        if c.track not in live:
            dead_canons += 1
            continue
        track_id = live[c.track]
        if track_id not in live_canons or c.decided_at > live_canons[track_id]:
            live_canons[track_id] = c.decided_at
    return Resolution(components, cannot_link, live_canons, outcomes, dead_canons)


MIRROR_VERSION = 2


def is_legacy(data: object) -> bool:
    """True for the old version-less mirror shape (a dict with no "version")."""
    return isinstance(data, dict) and "version" not in data


def _parse_time(value: object) -> datetime:
    """Parse an ISO `decided_at`; ValueError when unparsable or naive."""
    if not isinstance(value, str):
        raise ValueError(f"decided_at must be an ISO string, got {value!r}")
    at = datetime.fromisoformat(value)  # ValueError on garbage
    require_aware(at)
    return at


def dump_v2(pairs: Iterable[Pair], canons: Iterable[Canon]) -> dict:
    """Build the v2 mirror document, with pairs sorted by (a, b) and canons by track."""
    return {
        "version": MIRROR_VERSION,
        "pairs": [
            {
                "a": list(parse_key(p.a)),
                "b": list(parse_key(p.b)),
                "kind": p.kind.value,
                "decided_at": p.decided_at.isoformat(),
                "origin": p.origin.value,
            }
            for p in sorted(pairs, key=lambda p: (p.a, p.b))
        ],
        "canons": [
            {
                "track": list(parse_key(c.track)),
                "decided_at": c.decided_at.isoformat(),
                "origin": c.origin.value,
            }
            for c in sorted(canons, key=lambda c: c.track)
        ],
    }


def load_v2(data: dict) -> tuple[list[Pair], list[Canon]]:
    """Read a v2 mirror document into sorted pairs and canons; ValueError on anything malformed."""
    if not isinstance(data, dict) or data.get("version") != MIRROR_VERSION:
        raise ValueError(f"not a version {MIRROR_VERSION} decision document")
    try:
        pairs = [
            pair(
                key_text(parse_key(e["a"])),
                key_text(parse_key(e["b"])),
                DecisionKind(e["kind"]),
                _parse_time(e["decided_at"]),
                DecisionOrigin(e["origin"]),
            )
            for e in data["pairs"]
        ]
        canons = [
            Canon(key_text(parse_key(e["track"])), _parse_time(e["decided_at"]), DecisionOrigin(e["origin"]))
            for e in data["canons"]
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed decision document: {exc!r}") from exc
    return sorted(pairs, key=lambda p: (p.a, p.b)), sorted(canons, key=lambda c: c.track)
