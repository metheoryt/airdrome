"""Wanted / skip verdicts on tracks and aliases, resolved onto canon groups.

A track verdict is stored on the group's canon, an alias verdict on the alias itself. Reading folds
every verdict in a group (member tracks, plus aliases bound to any member) into one, where `SKIP`
beats `WANTED`. Domain functions only flush; the caller commits.
"""

import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from airdrome.enums import Verdict
from airdrome.library.edits import get_aliases, group_of
from airdrome.models import Track, TrackAlias, WantedVerdict


def _delete_group_verdicts(s: Session, member_ids: list[int], keep_track_id: int | None = None) -> int:
    """Delete verdicts on member tracks and on aliases bound to them, sparing one track row; returns count."""
    bound = select(TrackAlias.id).where(TrackAlias.track_id.in_(member_ids))
    on_tracks = WantedVerdict.track_id.in_([i for i in member_ids if i != keep_track_id])
    return s.execute(delete(WantedVerdict).where(on_tracks | WantedVerdict.alias_id.in_(bound))).rowcount


def set_track_verdict(s: Session, track_id: int, verdict: Verdict, note: str | None = None) -> WantedVerdict:
    """Record a verdict for the whole group of `track_id`, on its canon, replacing member verdicts."""
    group = group_of(s, track_id)
    canon = group[0]
    member_ids = [t.id for t in group]
    # An explicit decision about the group supersedes older verdicts on its members and bound aliases.
    _delete_group_verdicts(s, member_ids, keep_track_id=canon.id)
    row = s.scalar(select(WantedVerdict).where(WantedVerdict.track_id == canon.id))
    if row is None:
        row = WantedVerdict(track_id=canon.id, verdict=verdict, note=note)
        s.add(row)
    else:
        row.verdict, row.note = verdict, note
        row.decided_at = sa.func.now()
    s.flush()
    s.refresh(row)
    return row


def set_alias_verdict(
    s: Session, alias_ids: list[int], verdict: Verdict, note: str | None = None
) -> list[WantedVerdict]:
    """Record a verdict on each alias, one upserted row apiece."""
    aliases = get_aliases(s, alias_ids)
    existing = {
        r.alias_id: r
        for r in s.scalars(select(WantedVerdict).where(WantedVerdict.alias_id.in_([a.id for a in aliases])))
    }
    rows: list[WantedVerdict] = []
    for alias in aliases:
        row = existing.get(alias.id)
        if row is None:
            row = WantedVerdict(alias_id=alias.id, verdict=verdict, note=note)
            s.add(row)
        else:
            row.verdict, row.note = verdict, note
            row.decided_at = sa.func.now()
        rows.append(row)
    s.flush()
    for row in rows:
        s.refresh(row)
    return rows


def clear_verdict(s: Session, *, track_id: int | None = None, alias_ids: list[int] | None = None) -> int:
    """Delete a group's verdicts (tracks, bound aliases) and/or given aliases' verdicts; returns count."""
    deleted = 0
    if track_id is not None:
        ids = [t.id for t in group_of(s, track_id)]  # raises EditError for an unknown id
        deleted += _delete_group_verdicts(s, ids)
    if alias_ids:
        found = [a.id for a in get_aliases(s, alias_ids)]  # raises EditError for an unknown id
        deleted += s.execute(delete(WantedVerdict).where(WantedVerdict.alias_id.in_(found))).rowcount
    s.flush()
    return deleted


def _fold(rows: list[tuple[int, Verdict]]) -> dict[int, Verdict]:
    """Fold `(key, verdict)` pairs into one verdict per key, `SKIP` winning."""
    out: dict[int, Verdict] = {}
    for key, verdict in rows:
        if verdict == Verdict.SKIP or key not in out:
            out[key] = verdict
    return out


def group_verdicts(s: Session) -> dict[int, Verdict]:
    """Canon root id to effective verdict, over track verdicts and verdicts of bound aliases."""
    root = sa.func.coalesce(Track.canon_id, Track.id)
    by_track = select(root, WantedVerdict.verdict).join(Track, Track.id == WantedVerdict.track_id)
    # Join through `Track`, not `alias.track_id` alone: a merge can leave it on a twin.
    by_alias = (
        select(root, WantedVerdict.verdict)
        .join(TrackAlias, TrackAlias.id == WantedVerdict.alias_id)
        .join(Track, Track.id == TrackAlias.track_id)
    )
    rows = s.execute(by_track.union_all(by_alias)).all()
    return _fold([(r[0], r[1]) for r in rows])


def alias_verdicts(s: Session) -> dict[int, Verdict]:
    """Alias id to verdict, for aliases not bound to any track."""
    rows = s.execute(
        select(WantedVerdict.alias_id, WantedVerdict.verdict)
        .join(TrackAlias, TrackAlias.id == WantedVerdict.alias_id)
        .where(TrackAlias.track_id.is_(None))
    ).all()
    return {r[0]: r[1] for r in rows}
