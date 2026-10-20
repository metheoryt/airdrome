"""Session-level access to the decision tables: load, newest-wins upsert, import, export and resolve.

Also the mirror hand-off (`stash_mirror_snapshot`): the snapshot is taken here, inside the writing
transaction, and `mirror.py` writes it after the commit. Kept free of module-level mirror and legacy
imports so `persistence` and `mirror` can build on it without a cycle.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.models import CanonChoice, PairDecision, Track
from airdrome.normalize.dedup.decisions import (
    Canon,
    Outcome,
    Pair,
    Resolution,
    dump_v2,
    key_text,
    require_aware,
    resolve,
)


if TYPE_CHECKING:
    from airdrome.normalize.dedup.legacy import Converted


class Upsert(StrEnum):
    """What an upsert did with the incoming decision."""

    CREATED = "created"
    REPLACED = "replaced"
    KEPT = "kept"


def live_keys(s: Session) -> dict[str, int]:
    """Map key text -> track id for every track, from four columns (no entity load)."""
    rows = s.execute(select(Track.id, Track.title, Track.artist, Track.album, Track.album_artist))
    return {
        key_text((title, artist, album, album_artist)): tid
        for tid, title, artist, album, album_artist in rows
    }


def load_pairs(s: Session) -> list[Pair]:
    """Every stored pair decision, sorted in Python by (a, b) (SQL collation order differs)."""
    rows = s.scalars(select(PairDecision))
    pairs = [Pair(r.key_a, r.key_b, r.kind, r.decided_at, r.origin) for r in rows]
    return sorted(pairs, key=lambda p: (p.a, p.b))


def load_canons(s: Session) -> list[Canon]:
    """Every stored canon choice, sorted in Python by track key text."""
    return sorted(
        (Canon(r.key, r.decided_at, r.origin) for r in s.scalars(select(CanonChoice))), key=lambda c: c.track
    )


def upsert_pair(s: Session, p: Pair) -> Upsert:
    """Store `p`, replacing an existing row for the same ends only if `p` is strictly newer."""
    require_aware(p.decided_at)
    if not p.a < p.b:  # build pairs with `pair()`; a backwards one would add a second row for the same ends
        raise ValueError("a pair must have a < b")
    row = s.scalars(select(PairDecision).filter_by(key_a=p.a, key_b=p.b)).one_or_none()
    if row is None:
        s.add(PairDecision(key_a=p.a, key_b=p.b, kind=p.kind, decided_at=p.decided_at, origin=p.origin))
        s.flush()
        return Upsert.CREATED
    if p.decided_at <= row.decided_at:
        return Upsert.KEPT
    row.kind, row.decided_at, row.origin = p.kind, p.decided_at, p.origin
    s.flush()
    return Upsert.REPLACED


def upsert_canon(s: Session, c: Canon) -> Upsert:
    """Store `c`, replacing an existing row for the same track only if `c` is strictly newer."""
    require_aware(c.decided_at)
    row = s.scalars(select(CanonChoice).filter_by(key=c.track)).one_or_none()
    if row is None:
        s.add(CanonChoice(key=c.track, decided_at=c.decided_at, origin=c.origin))
        s.flush()
        return Upsert.CREATED
    if c.decided_at <= row.decided_at:
        return Upsert.KEPT
    row.decided_at, row.origin = c.decided_at, c.origin
    s.flush()
    return Upsert.REPLACED


def export_decisions(s: Session) -> dict[str, object]:
    """The v2 mirror document for both tables."""
    return dump_v2(load_pairs(s), load_canons(s))


def resolve_library(s: Session) -> Resolution:
    """Resolve the stored decisions against the library's live tracks."""
    return resolve(load_pairs(s), load_canons(s), live_keys(s))


def decision_counts(s: Session) -> dict[str, int]:
    """Tally the stored decisions against the library: live pairs by kind, resolver outcomes, canons.

    `same`/`different` count every non-dead pair row of that kind (a superseded one included);
    `dead` is pairs plus canons that name a track no longer in the library.
    """
    res = resolve_library(s)
    live = [p for p in load_pairs(s) if res.outcomes[(p.a, p.b)] is not Outcome.DEAD]
    dead_pairs = sum(o is Outcome.DEAD for o in res.outcomes.values())
    return {
        "same": sum(p.kind == DecisionKind.SAME for p in live),
        "different": sum(p.kind == DecisionKind.DIFFERENT for p in live),
        "canons": len(res.canons),
        "superseded": sum(o is Outcome.SUPERSEDED for o in res.outcomes.values()),
        "dead": dead_pairs + res.dead_canons,
    }


# --- durable-mirror hand-off -----------------------------------------------
# `mirror.py` writes the stashed document after the commit; its module docstring explains the split.
MIRROR_SNAPSHOT_KEY = "dedup_mirror"


def stash_mirror_snapshot(s: Session) -> None:
    """Record the post-flush decision tables (a v2 document) for the after-commit mirror write."""
    s.info[MIRROR_SNAPSHOT_KEY] = export_decisions(s)


@dataclass
class ImportCounts:
    """What an import did, over pairs and canons together."""

    created: int = 0
    replaced: int = 0
    kept: int = 0

    def add(self, outcome: Upsert) -> None:
        """Count one upsert outcome."""
        if outcome is Upsert.CREATED:
            self.created += 1
        elif outcome is Upsert.REPLACED:
            self.replaced += 1
        else:
            self.kept += 1


def import_decisions(s: Session, pairs: Iterable[Pair], canons: Iterable[Canon]) -> ImportCounts:
    """Upsert decisions newest-wins row by row, then stash the mirror snapshot if any row changed.

    A pair (by its ends) or a canon (by its track) listed twice is a ValueError before anything is
    written: with equal timestamps the upsert would keep whichever came first, so the result would
    depend on the input's order. A `decided_at` later than now (a file from a box whose clock ran fast)
    is clamped to now, so it can never outrank the owner's next edit.
    """
    limit = datetime.now(UTC)
    pairs = [p if p.decided_at <= limit else replace(p, decided_at=limit) for p in pairs]
    canons = [c if c.decided_at <= limit else replace(c, decided_at=limit) for c in canons]
    ends = [(p.a, p.b) for p in pairs]
    if len(set(ends)) != len(ends):
        raise ValueError("the decisions list a pair more than once")
    tracks = [c.track for c in canons]
    if len(set(tracks)) != len(tracks):
        raise ValueError("the decisions list a canon choice for one track more than once")
    counts = ImportCounts()
    for p in pairs:
        counts.add(upsert_pair(s, p))
    for c in canons:
        counts.add(upsert_canon(s, c))
    if counts.created or counts.replaced:
        # An import that changed no row leaves the mirror alone: rewriting it would at best be a no-op,
        # and at worst (a stray or mistaken import into empty tables) replace the file with an empty one.
        stash_mirror_snapshot(s)
    return counts


def import_legacy(s: Session, data: dict, now: datetime) -> tuple[ImportCounts, Converted]:
    """Import an old version-less group file: of `n` groups, group `i` is decided at `now - (n-1-i)` µs.

    Later file entries are later decisions, and the last is decided at `now`, never after it. Origin IMPORT.

    Each group converts as the migration does (same canon-choice rule), against the live tracks. A pair
    or canon repeated across groups keeps the later group's row (it is the newer decision). Returns the
    import counts and the summed conversion counters, with the collapsed decisions.
    """
    from airdrome.normalize.dedup.legacy import (
        Converted,
        canon_pick,
        convert_group,
        legacy_groups,
        live_tracks,
    )

    require_aware(now)
    groups = legacy_groups(data)
    live = live_tracks(s.connection())
    pick = canon_pick(s.connection(), live)
    total = Converted()
    pairs: dict[tuple[str, str], Pair] = {}
    canons: dict[str, Canon] = {}
    for i, g in enumerate(groups):
        conv = convert_group(
            g,
            live.keys_of_hash,
            pick,
            now - timedelta(microseconds=len(groups) - 1 - i),
            DecisionOrigin.IMPORT,
        )
        pairs.update({(p.a, p.b): p for p in conv.pairs})  # file order: the later group overwrites
        canons.update({c.track: c for c in conv.canons})
        total.dropped += conv.dropped
        total.ambiguous += conv.ambiguous
        total.dangling_canon += conv.dangling_canon
        total.conflicting_canons += conv.conflicting_canons
    total.pairs = sorted(pairs.values(), key=lambda p: (p.a, p.b))
    total.canons = sorted(canons.values(), key=lambda c: c.track)
    return import_decisions(s, total.pairs, total.canons), total
