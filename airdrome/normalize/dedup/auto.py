"""Automatic dedup over the library, layered with the manual pair decisions.

`plan_dedup` computes the whole outcome without writing: final groups (canon first), each twin's link
and which twins are folded in (outside the canon's core). `apply_plan` writes it for all tracks or for
a scope, which is how an MCP point edit reproduces exactly what a full `dedup` run would.
"""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from airdrome.console import detail
from airdrome.enums import BindMethod
from airdrome.models import Track

from .decisions import Outcome, Resolution
from .grouping import CanonStrategy, canon_order, merge_overlapping_groups
from .persistence import flatten_canon_chains, recompute_main_files
from .recording import scan_recordings
from .store import resolve_library


def compute_auto_dedup_groups(
    session: Session,
    with_artist: bool = True,
    with_album_artist: bool = True,
    with_album: bool = True,
    with_year: bool = True,
    with_track_n: bool = True,
    with_disc_n: bool = True,
    with_duration: bool = True,
    strategy: CanonStrategy = CanonStrategy.ADDED,
) -> list[list[Track]]:
    """Return duplicate groups for a single flag-set; no writes.

    Tracks within a group are sorted by canon priority (see `canon_order`) so
    group[0] is the candidate canon; `strategy` picks which key leads. Title is
    always required; the other fields can each be excluded to loosen matching.
    """
    tracks = list(session.scalars(select(Track).order_by(*canon_order(strategy))))

    def is_blank(v: object) -> bool:
        return v is None or (isinstance(v, str) and not v.strip())

    def key_parts(t: Track) -> list:
        parts: list = []
        if with_artist:
            parts.append(t.artist_norm)
        if with_album_artist:
            parts.append(t.album_artist_norm)
        if with_album:
            parts.append(t.album_norm)
        if with_track_n:
            parts.append(t.track_n)
        if with_disc_n:
            parts.append(t.disc_n)
        if with_duration:
            parts.append(round(t.duration / 5) * 5 if t.duration is not None else None)
        if with_year:
            parts.append(t.year)
        return parts

    bucketed: dict[tuple, list[Track]] = {}
    for t in tracks:
        if is_blank(t.title_norm):  # title is the always-required key
            continue
        parts = key_parts(t)
        # If every selected field is blank, the key degenerates to ~title alone
        # and would collapse unrelated same-title tracks into one bogus group;
        # skip this track for this set (a looser set may still surface it).
        if parts and all(is_blank(p) for p in parts):
            continue
        bucketed.setdefault((t.title_norm, *parts), []).append(t)

    return [g for g in bucketed.values() if len(g) >= 2]


@dataclass
class DedupPlan:
    """The outcome of one dedup pass, before anything is written."""

    groups: list[list[Track]]  # final groups, canon first
    links: dict[int, tuple[int, BindMethod, float | None]]  # twin id -> (canon id, method, confidence)
    folded: frozenset[int]  # twins outside their canon's core (joined only through a manual decision)
    raw: list[tuple[str, list[Track]]]  # every input group: manual components, recording, flag-sets
    resolution: Resolution
    conflicted: list[Track] = field(default_factory=list)  # tracks whose files disagree on the id


def _flag_set_groups(
    s: Session, flag_sets: list[dict[str, bool]], strategy: CanonStrategy
) -> list[tuple[str, list[Track]]]:
    """Every flag-set's bucket groups, labelled by set index and the fields it leaves out."""
    out: list[tuple[str, list[Track]]] = []
    for i, flags in enumerate(flag_sets):
        excluded = sorted(k.removeprefix("with_") for k, v in flags.items() if v is False)
        key_suffix = "-" + "-".join(excluded) if excluded else "all"
        for j, group in enumerate(compute_auto_dedup_groups(s, strategy=strategy, **flags)):
            out.append((f"set{i}[{key_suffix}]#{j}", group))
    return out


def plan_dedup(s: Session, flag_sets: list[dict[str, bool]], strategy: CanonStrategy) -> DedupPlan:
    """Compute the final groups from decisions, recording identity and flag-sets; no writes.

    Manual must-link components go into the union-find first, so manual wins both ways: an automatic
    join to a track a manual `different` separates is vetoed, and a manual `same` is never vetoed (and
    may cross recording ids). The canon is the member with the newest canon choice, else the first in
    `canon_order`. Its core is what automatic grouping alone joins to it; the rest of the group is folded.
    Reads no `canon_id`, so it needs no reset first.
    """
    res = resolve_library(s)
    rec = scan_recordings(s, strategy)

    # Each component in canon order (an intact single group passes through the merge as-is), all of them
    # from one query: the query's canon order, partitioned by component, keeps each component's order.
    manual_of = {tid: i for i, comp in enumerate(res.components) for tid in comp}
    members: list[list[Track]] = [[] for _ in res.components]
    in_components = select(Track).where(Track.id.in_(list(manual_of))).order_by(*canon_order(strategy))
    for t in s.scalars(in_components) if manual_of else ():
        members[manual_of[t.id]].append(t)
    manual = [(f"manual#{i}", tracks) for i, tracks in enumerate(members)]
    auto_raw = [*rec.auto, *_flag_set_groups(s, flag_sets, strategy)]
    raw = [*manual, *auto_raw]
    merged = merge_overlapping_groups(s, raw, strategy, cannot_link=res.cannot_link)

    # The automatic part alone, under the same veto: each track's core candidates.
    auto_comp: dict[int, frozenset[int]] = {}
    for _, group in merge_overlapping_groups(s, auto_raw, strategy, cannot_link=res.cannot_link):
        ids = frozenset(t.id for t in group)
        for tid in ids:
            auto_comp[tid] = ids
    rec_group_of = {t.id: j for j, (_, group) in enumerate(rec.auto) for t in group}
    rec_members = [{t.id for t in group} for _, group in rec.auto]

    groups: list[list[Track]] = []
    links: dict[int, tuple[int, BindMethod, float | None]] = {}
    folded: set[int] = set()
    for _, group in merged:
        # Newest canon choice wins; at an equal time the one earlier in canon order (a fixed tie-break).
        chosen = [(i, t) for i, t in enumerate(group) if t.id in res.canons]
        if chosen:
            _, canon = max(chosen, key=lambda it: (res.canons[it[1].id], -it[0]))
        else:
            canon = group[0]
        group = [canon, *(t for t in group if t is not canon)]
        groups.append(group)
        ids = {t.id for t in group}
        core = ids & auto_comp.get(canon.id, frozenset((canon.id,)))
        for twin in group[1:]:
            in_manual = twin.id in manual_of and manual_of[twin.id] == manual_of.get(canon.id)
            if twin.id not in core:
                folded.add(twin.id)
            if twin.id not in core or in_manual:
                links[twin.id] = (canon.id, BindMethod.MANUAL, 1.0)
                continue
            # Recording evidence names the link when the twin's recording group reaches into this
            # group through another member; otherwise only the flag-sets joined it.
            rec_j = rec_group_of.get(twin.id)
            if rec_j is not None and len(rec_members[rec_j] & ids) > 1:
                method, confidence = rec.evidence[twin.id]
                links[twin.id] = (canon.id, method, confidence)
            else:
                links[twin.id] = (canon.id, BindMethod.DEDUP_FLAGS, None)

    return DedupPlan(groups, links, frozenset(folded), raw, res, list(rec.conflicted))


def apply_plan(s: Session, plan: DedupPlan, scope: AbstractSet[int] | None = None) -> set[int]:
    """Write the plan's links onto the tracks in `scope` (all when None); returns old and new root ids.

    Writes `canon` (through the relationship, so loaded `twins` collections follow), `canon_method`,
    `canon_confidence` and `canon_folded`, roots included (a root carries no link). Flushes.
    """
    query = select(Track)
    if scope is not None:
        query = query.where(Track.id.in_(list(scope)))
    tracks = {t.id: t for t in s.scalars(query)}
    touched: set[int] = set()
    for tid, t in tracks.items():
        touched.add(t.canon_id if t.canon_id is not None else tid)
        link = plan.links.get(tid)
        target = link[0] if link else None
        if t.canon_id != target:
            t.canon = (tracks.get(target) or s.get_one(Track, target)) if target is not None else None
        t.canon_method, t.canon_confidence = (link[1], link[2]) if link else (None, None)
        t.canon_folded = tid in plan.folded
        touched.add(target if target is not None else tid)
    s.flush()
    # A many-to-one write whose old value was never loaded leaves that old root's loaded `twins` stale,
    # and `recompute_main_files` reads `twins`; reload them.
    for root_id in touched:
        root = s.get(Track, root_id)
        if root is not None:
            s.expire(root, ["twins"])
    return touched


def _current_groups(s: Session) -> dict[int, set[int]]:
    """Root id -> ids of its current group (root and twins), from `canon_id`."""
    groups: dict[int, set[int]] = {}
    for tid, cid in s.execute(select(Track.id, Track.canon_id)):
        groups.setdefault(cid if cid is not None else tid, set()).add(tid)
    return groups


def edit_scope(s: Session, plan: DedupPlan, seeds: AbstractSet[int]) -> set[int]:
    """Close `seeds` to a fixpoint over the plan's raw groups and the current canon groups.

    The result is a union of whole final groups (every final group is a union of raw groups) and of
    whole current groups, so applying the plan to it reproduces a full run on those tracks and leaves
    every other track's group as it is.
    """
    by_track: dict[int, list[set[int]]] = {}
    for _, group in plan.raw:
        ids = {t.id for t in group}
        for tid in ids:
            by_track.setdefault(tid, []).append(ids)
    for members in _current_groups(s).values():
        for tid in members:
            by_track.setdefault(tid, []).append(members)

    scope = set(seeds)
    todo = list(scope)
    while todo:
        tid = todo.pop()
        for ids in by_track.get(tid, ()):
            for other in ids - scope:
                scope.add(other)
                todo.append(other)
    return scope


@dataclass
class AutoDedupResult:
    groups: list[list[Track]]
    auto_twins: int  # twins linked by automatic evidence (recording identity or flag-sets)
    manual_links: int  # twins linked MANUAL (folded in, or sharing a manual component with the canon)
    superseded: int = 0  # pair decisions a newer decision overrides
    dead: int = 0  # pair decisions and canon choices naming a track no longer in the library
    conflicted: int = 0  # tracks left out of recording grouping: their files disagree on the id


def auto_deduplicate(
    session: Session,
    flag_sets: list[dict[str, bool]] | None = None,
    strategy: CanonStrategy = CanonStrategy.ADDED,
) -> AutoDedupResult:
    """Rebuild Track.canon_id from the manual decisions, recording identity and flag-sets.

    Every run starts clean: all canon links (method, confidence, folded flag) are reset, then
    `plan_dedup` decides every group and `apply_plan` writes it; a final pass flattens any canon chain
    and re-picks each group's main file. `strategy` picks which member of each group becomes canon
    when no canon choice does. Caller is responsible for committing.
    """
    if not flag_sets:
        flag_sets = [{}]

    session.execute(
        update(Track).values(canon_id=None, canon_method=None, canon_confidence=None, canon_folded=False)
    )
    session.flush()
    session.expire_all()  # the bulk update leaves loaded `canon` / `twins` relationships stale

    plan = plan_dedup(session, flag_sets, strategy)
    for t in plan.conflicted:
        ids = ", ".join(sorted({f.recording_mbid for f in t.files if f.recording_mbid}))
        detail(f"  [yellow]conflicting recording ids[/yellow] {t.artist} — {t.title} ({t.album}): {ids}")
    apply_plan(session, plan)
    flatten_canon_chains(session)
    # The graph was fully rebuilt; re-pick each group's main across canon + twins.
    recompute_main_files(session)

    manual = sum(1 for _, method, _ in plan.links.values() if method is BindMethod.MANUAL)
    outcomes = plan.resolution.outcomes.values()
    return AutoDedupResult(
        groups=plan.groups,
        auto_twins=len(plan.links) - manual,
        manual_links=manual,
        superseded=sum(o is Outcome.SUPERSEDED for o in outcomes),
        dead=sum(o is Outcome.DEAD for o in outcomes) + plan.resolution.dead_canons,
        conflicted=len(plan.conflicted),
    )
