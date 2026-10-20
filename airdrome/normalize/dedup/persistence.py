from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from airdrome.models import Track, TrackGroup


def flatten_canon_chains(session: Session) -> int:
    """Repoint any canon_id whose target is itself a twin to the root.

    Enforces the flat invariant documented on Track.canon_id so every reader
    can resolve canonicality with a single hop. Idempotent; asserts no chain
    remains.
    """
    pairs = session.execute(select(Track.id, Track.canon_id)).all()
    canon_of = dict(pairs)

    changed = 0
    for tid, cid in pairs:
        if cid is None:
            continue
        seen = {tid}
        root = cid
        while True:
            nxt = canon_of.get(root)
            if nxt is None or root in seen:
                break
            seen.add(root)
            root = nxt
        if root != cid:
            session.execute(update(Track).where(Track.id == tid).values(canon_id=root))
            changed += 1
    if changed:
        session.flush()

    canon = aliased(Track)
    remaining = session.scalar(
        select(func.count())
        .select_from(Track)
        .join(canon, Track.canon_id == canon.id)
        .where(canon.canon_id.is_not(None))
    )
    assert remaining == 0, f"canon chains remain after flatten: {remaining}"
    return changed


def recompute_main_files(session: Session, root_ids: Iterable[int] | None = None) -> int:
    """Re-pick the single `is_main` file for every dedup group (or only the groups rooted at `root_ids`).

    A group's main file must be the best copy among the canon's core (the whole group when the core
    has no file, see `TrackGroup.main_candidates`); when the canon graph changes (a `dedup` run or
    an MCP point edit) a merge can
    leave two already-organized files marked main, or the best copy can land on
    a member that no longer owns the flag. Walk every canon root (canon_id IS
    NULL — true canons and singletons alike) and let `TrackGroup.recompute_main`
    re-mark exactly one file per group. Returns the number of groups whose main
    file changed; caller commits. (This only updates the flag — files already on
    disk are not relocated; physical re-placement is the reconcile roadmap.)

    `root_ids` scopes the walk to the groups a point edit touched; an id that is no longer a root is
    skipped (its files now belong to its new root's group, which the caller passes too).
    """
    session.flush()  # make pending canon_id writes visible to the root query
    changed = 0
    roots = select(Track).where(Track.canon_id.is_(None))
    if root_ids is not None:
        roots = roots.where(Track.id.in_(list(root_ids)))
    for root in session.scalars(roots):
        group = TrackGroup.of(root)
        # Compare the full set so a cleared stale second main also counts.
        before = {f.id for m in group.members for f in m.files if f.is_main}
        chosen = group.recompute_main()
        if chosen is not None and before != {chosen.id}:
            changed += 1
    session.flush()
    return changed
