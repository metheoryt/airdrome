from collections.abc import Set as AbstractSet
from enum import StrEnum

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from airdrome.models import Track


class CanonStrategy(StrEnum):
    """Which track in a duplicate group is preferred as canon (group[0])."""

    ADDED = "added"  # the copy added earliest wins
    YEAR = "year"  # the earliest-released copy wins


# The optional bucket-key fields (title is always implicit). Single source of
# truth for both the CLI flag parsing and the manual deduplicator's defaults.
FIELDS = ("artist", "album_artist", "album", "year", "track_n", "disc_n", "duration")


def flag_set(*active: str) -> dict[str, bool]:
    """Build a `compute_auto_dedup_groups` flag-set with only `active` fields on."""
    return {f"with_{f}": (f in active) for f in FIELDS}


# Recommended auto-dedup flag-sets: precise yet mass. Each gates every merge on a matching
# duration OR year, so edits/remixes/live cuts don't collapse — reaching ~98% of the reckless
# artist-only ceiling without its false merges (see CLAUDE.md "Dedup tuning notes"). Used as the
# `dedup` CLI default; the library function keeps its own all-fields fallback.
RECOMMENDED_SETS = [
    flag_set("artist", "duration"),
    flag_set("artist", "year"),
    flag_set("album_artist", "duration"),
]


# Album-artist values (normalized) that mark a various-artists compilation, beside the
# `compilation` flag itself.
VARIOUS_ARTISTS = ("various artists", "various")


def _is_compilation(t: type[Track]) -> ColumnElement[bool]:
    """SQL predicate: `t` is a compilation member (flagged, or credited to various artists)."""
    return or_(t.compilation.is_(True), t.album_artist_norm.in_(VARIOUS_ARTISTS))


def _compilation_rank() -> ColumnElement[int]:
    """Leading canon key: 0 = a compilation the owner has >= 2 tracks of, 1 = not a compilation,
    2 = a compilation the owner has a single track of.

    The owner's rule (docs/design/identity-ladder.md, *Canon choice*): prefer the original album,
    but a compilation/OST wins when the owner has >= 2 tracks from it. A SQL `CASE` over a
    correlated per-album count, so the query-time order and `merge_overlapping_groups`' re-query
    agree by construction. A blank album is no compilation identity, so it never counts.
    """
    other = aliased(Track)
    per_album = (
        select(func.count())
        .where(other.album_norm == Track.album_norm, other.album_norm != "", _is_compilation(other))
        .correlate(Track)
        .scalar_subquery()
    )
    is_comp = _is_compilation(Track)
    return case((and_(is_comp, per_album >= 2), 0), (is_comp, 2), else_=1)


def canon_order(strategy: CanonStrategy = CanonStrategy.ADDED) -> list:
    """Return order_by clauses placing the preferred canon first.

    The compilation rank leads (see `_compilation_rank`). `loved` is intentionally excluded: a
    group's loved status is derived from the whole group, so it must not decide which member is
    canon. `id` is the final, stable tiebreaker. The chosen strategy's key comes next; the other
    date key follows as a tiebreaker.
    """
    added = Track.date_added.asc().nulls_last()
    year = Track.year.asc().nulls_last()
    if strategy is CanonStrategy.YEAR:
        return [_compilation_rank(), year, added, Track.id]
    return [_compilation_rank(), added, year, Track.id]


def merge_overlapping_groups(
    session: Session,
    groups: list[tuple[str, list[Track]]],
    strategy: CanonStrategy = CanonStrategy.ADDED,
    cannot_link: AbstractSet[frozenset[int]] = frozenset(),
) -> list[tuple[str, list[Track]]]:
    """Union-find over tracks: any two groups sharing a track collapse into one page.

    Surfacing the full component together lets the user resolve all canon
    picks for those tracks in one place — and structurally prevents
    cross-page picks from creating canon chains (e.g. T3->T2 chosen on
    one page and T2->T1 on another). Multi-group components are re-queried
    so members come back in canon-priority order.

    `cannot_link` holds track-id pairs that must never share a component (a stored "not
    duplicates" choice). The union runs per track, not per group, so the veto is transitive: a
    union is refused when it would put any vetoed pair together, directly or through a third
    track, and every track still lands in exactly one component. A group a veto splits comes back
    as its surviving pieces; a piece of one track is dropped.
    """
    if not groups:
        return []

    partners: dict[int, set[int]] = {}
    for pair in cannot_link:
        a, b = tuple(pair)
        partners.setdefault(a, set()).add(b)
        partners.setdefault(b, set()).add(a)

    parent: dict[int, int] = {}
    members: dict[int, set[int]] = {}  # root -> the component's track ids

    def find(x: int) -> int:
        """Root of `x`'s component, halving the path as it goes."""
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        """Join the components of `a` and `b`, unless that would co-locate a cannot-link pair."""
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        small, large = sorted((members[ra], members[rb]), key=len)
        # Components are disjoint, so every cross pair would be new: check them all.
        if any(not partners.get(x, set()).isdisjoint(large) for x in small):
            return
        parent[ra] = rb
        members[rb] |= members.pop(ra)

    group_ids: list[list[int]] = []
    for _, tracks in groups:
        ids = [t.id for t in tracks if t.id is not None]
        group_ids.append(ids)
        for tid in ids:
            if tid not in parent:
                parent[tid] = tid
                members[tid] = {tid}
        # A star from the group's first member (its canon candidate): under a veto, the pieces
        # that stay are the ones joined to that candidate.
        for tid in ids[1:]:
            union(ids[0], tid)

    # Which groups feed each component, in input order.
    sources: dict[int, list[int]] = {}
    for i, ids in enumerate(group_ids):
        for root in dict.fromkeys(find(tid) for tid in ids):
            sources.setdefault(root, []).append(i)

    merged: list[tuple[str, list[Track]]] = []
    emitted: set[int] = set()
    for i, ids in enumerate(group_ids):
        for root in dict.fromkeys(find(tid) for tid in ids):
            if root in emitted:
                continue
            emitted.add(root)
            src = sources[root]
            key = " + ".join(sorted({groups[idx][0] for idx in src}))
            if len(src) == 1 and members[root] == set(ids):
                merged.append((key, groups[i][1]))  # an intact single group passes through as-is
                continue
            if len(members[root]) < 2:
                continue
            tracks = list(
                session.scalars(
                    select(Track).where(Track.id.in_(members[root])).order_by(*canon_order(strategy))
                )
            )
            merged.append((key, tracks))

    return merged
