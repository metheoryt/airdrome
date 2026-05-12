from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from airdrome.conf import settings
from airdrome.models import Track
from airdrome.normalize.dedup import Deduplicator, compute_auto_dedup_groups, flatten_canon_chains
from airdrome.normalize.dedup_history import AUTO_DEDUP_FLAG_KEYS, load_history


@dataclass
class MaterializeStats:
    history_entries: int
    auto_components: int
    auto_twins: int
    manual_changes: int
    chain_rewrites: int


def materialize(session: Session) -> MaterializeStats:
    """Rebuild Track.canon_id from auto-dedup history + duplicates.json.

    Auto-dedup history is collapsed via union-find: every grouping decision
    from every recorded flag-set contributes edges, then each connected
    component is given one canon (earliest by date_added → year → loved → id).
    Manual decisions from duplicates.json are applied as overrides on top.
    A final chain-flatten pass guarantees twin.canon_id always points to a
    root track (canon_id IS NULL). The result is order-independent.

    Caller is responsible for committing the session (or rolling back via
    AppState dry-run).
    """
    session.execute(update(Track).values(canon_id=None))
    session.flush()

    all_tracks = list(
        session.scalars(
            select(Track).order_by(
                Track.date_added.asc().nulls_last(),
                Track.year.asc().nulls_last(),
                Track.loved.desc().nulls_last(),
                Track.id,
            )
        )
    )

    parent: dict[int, int] = {t.id: t.id for t in all_tracks}
    rank: dict[int, int] = {t.id: 0 for t in all_tracks}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    history = load_history(settings.auto_dedup_history_filepath)
    for entry in history:
        flags = {k: v for k, v in entry["flags"].items() if k in AUTO_DEDUP_FLAG_KEYS}
        for group in compute_auto_dedup_groups(session, **flags):
            anchor = group[0].id
            for t in group[1:]:
                union(anchor, t.id)

    components: dict[int, list[Track]] = {}
    for t in all_tracks:
        components.setdefault(find(t.id), []).append(t)

    auto_twins = 0
    auto_components = 0
    for component_tracks in components.values():
        if len(component_tracks) < 2:
            continue
        auto_components += 1
        # all_tracks is in canon-priority order, so component_tracks[0] is the canon.
        canon = component_tracks[0]
        for twin in component_tracks[1:]:
            twin.canon_id = canon.id
            session.add(twin)
            auto_twins += 1
    session.flush()

    dedup = Deduplicator(session, filepath=settings.duplicates_filepath)
    dedup.fill_state()
    manual_changes = dedup.apply_changes()
    session.flush()

    # Manual edges can connect two auto components; the side whose canon
    # becomes a twin leaves its previous twins dangling in a chain.
    chain_rewrites = flatten_canon_chains(session)

    return MaterializeStats(
        history_entries=len(history),
        auto_components=auto_components,
        auto_twins=auto_twins,
        manual_changes=manual_changes,
        chain_rewrites=chain_rewrites,
    )
