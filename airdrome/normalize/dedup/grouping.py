from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.models import Track


def merge_overlapping_groups(
    session: Session,
    groups: list[tuple[str, list[Track]]],
) -> list[tuple[str, list[Track]]]:
    """Union-find over groups: any two sharing a track collapse into one page.

    Surfacing the full component together lets the user resolve all canon
    picks for those tracks in one place — and structurally prevents
    cross-page picks from creating canon chains (e.g. T3->T2 chosen on
    one page and T2->T1 on another). Multi-group components are re-queried
    so members come back in canon-priority order.
    """
    n = len(groups)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    first_group: dict[int, int] = {}
    for i, (_, tracks) in enumerate(groups):
        for t in tracks:
            if t.id is None:
                continue
            if t.id in first_group:
                union(first_group[t.id], i)
            else:
                first_group[t.id] = i

    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    merged: list[tuple[str, list[Track]]] = []
    emitted: set[int] = set()
    for i, _ in enumerate(groups):
        root = find(i)
        if root in emitted:
            continue
        emitted.add(root)
        member_idxs = components[root]
        key = " + ".join(sorted({groups[idx][0] for idx in member_idxs}))
        if len(member_idxs) == 1:
            merged.append((key, groups[member_idxs[0]][1]))
            continue
        ids = {t.id for idx in member_idxs for t in groups[idx][1] if t.id is not None}
        tracks = list(
            session.scalars(
                select(Track)
                .where(Track.id.in_(ids))
                .order_by(
                    Track.date_added.asc().nulls_last(),
                    Track.year.asc().nulls_last(),
                    Track.loved.desc().nulls_last(),
                    Track.id,
                )
            )
        )
        merged.append((key, tracks))

    return merged
