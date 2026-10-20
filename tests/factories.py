"""Shared test factories for dedup and related tests.

Thin helpers over the SQLAlchemy models — each returns a flushed object so
tests can use `.id` (and `old_hash`) immediately. Defaults are chosen
to satisfy the `Track.UniqueConstraint(title, artist, album, album_artist)`. That constraint is
NULLS NOT DISTINCT, so two tracks with the same title and all-None other fields collide: give
tracks that must coexist a distinct title, artist or album.
"""

import itertools
from datetime import UTC, datetime, timedelta

from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.models import CanonChoice, PairDecision, Track
from airdrome.normalize.dedup.decisions import key_text, pair, track_key
from airdrome.normalize.dedup.legacy import legacy_hash


# Default decision times come from a fixed past base so later (real, "now") edits always beat fixtures.
_BASE = datetime(2020, 1, 1, tzinfo=UTC)
_clock = itertools.count(1)


def _next_time() -> datetime:
    """Return the next strictly increasing fixture timestamp."""
    return _BASE + timedelta(seconds=next(_clock))


def make_track(
    s,
    title: str = "t",
    artist: str | None = None,
    album: str | None = None,
    album_artist: str | None = None,
    **kw,
) -> Track:
    t = Track(title=title, artist=artist, album=album, album_artist=album_artist, **kw)
    s.add(t)
    s.flush()
    return t


def old_hash(t: Track) -> str:
    """The track's hash in the old stored-group format (what `Track.duplicate_hash` used to return)."""
    return legacy_hash(t.title, t.artist, t.album_artist, t.album, t.track_n, t.disc_n, t.year, t.duration)


def add_pair(
    s,
    t1: Track,
    t2: Track,
    kind: DecisionKind = DecisionKind.SAME,
    at: datetime | None = None,
    origin: DecisionOrigin = DecisionOrigin.MCP,
) -> PairDecision:
    """Store a pair decision about two tracks (ends ordered like `pair()`), flushed."""
    p = pair(key_text(track_key(t1)), key_text(track_key(t2)), kind, at or _next_time(), origin)
    row = PairDecision(key_a=p.a, key_b=p.b, kind=p.kind, decided_at=p.decided_at, origin=p.origin)
    s.add(row)
    s.flush()
    return row


def add_canon(s, t: Track, at: datetime | None = None) -> CanonChoice:
    """Store a canon choice for a track, flushed."""
    row = CanonChoice(key=key_text(track_key(t)), decided_at=at or _next_time(), origin=DecisionOrigin.MCP)
    s.add(row)
    s.flush()
    return row
