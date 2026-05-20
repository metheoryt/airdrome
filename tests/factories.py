"""Shared test factories for dedup and related tests.

Thin helpers over the SQLAlchemy models — each returns a flushed object so
tests can use `.id` and `.duplicate_hash` immediately. Defaults are chosen
to satisfy the `Track.UniqueConstraint(title, artist, album, album_artist)`
naturally (NULL/None values are treated as distinct by Postgres).
"""

from airdrome.models import DedupGroup, DedupGroupMember, Track
from airdrome.normalize.dedup.manual import Page


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


def make_dedup_group(
    s,
    members: list[tuple[Track, Track | None]],
    label: str | None = None,
) -> DedupGroup:
    """Build a stored group from (member_track, canon_track_or_None) pairs.

    `canon_track=None` ⇒ `canon_hash=NULL` (member is canon or independent).
    """
    group = DedupGroup(label=label)
    group.members = [
        DedupGroupMember(
            member_hash=m.duplicate_hash,
            canon_hash=(c.duplicate_hash if c is not None else None),
        )
        for m, c in members
    ]
    s.add(group)
    s.flush()
    return group


def make_page(tracks: list[Track]) -> Page:
    return Page(tracks=tracks)
