from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from airdrome.models import Track

from .grouping import CanonStrategy, canon_order, merge_overlapping_groups
from .persistence import apply_manual_overrides, flatten_canon_chains


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
class AutoDedupResult:
    groups: list[list[Track]]
    auto_twins: int
    manual_changes: int


def auto_deduplicate(
    session: Session,
    flag_sets: list[dict[str, bool]] | None = None,
    strategy: CanonStrategy = CanonStrategy.ADDED,
) -> AutoDedupResult:
    """Rebuild Track.canon_id from one or more flag-sets + stored manual overrides.

    Every run starts clean: all canon_ids are reset, each flag-set produces
    its own bucket-grouping, overlapping groups across sets merge via
    union-find so a track ends up with one canon, then stored manual choices
    layer on top (overriding any auto canon they touch), and a final pass
    flattens any canon chain the overlay may have introduced. `strategy` picks
    which member of each group becomes canon. Caller is responsible for
    committing.
    """
    if not flag_sets:
        flag_sets = [{}]

    # Reset before computing groups so the in-memory Track objects this
    # function returns reflect DB state (no stale canon_id values).
    session.execute(update(Track).values(canon_id=None))
    session.flush()

    raw: list[tuple[str, list[Track]]] = []
    for i, flags in enumerate(flag_sets):
        groups = compute_auto_dedup_groups(session, strategy=strategy, **flags)
        excluded = sorted(k.removeprefix("with_") for k, v in flags.items() if v is False)
        key_suffix = "-" + "-".join(excluded) if excluded else "all"
        for j, group in enumerate(groups):
            raw.append((f"set{i}[{key_suffix}]#{j}", group))

    merged = merge_overlapping_groups(session, raw, strategy)

    auto_twins = 0
    out_groups: list[list[Track]] = []
    for _, group in merged:
        out_groups.append(group)
        canon = group[0]
        for twin in group[1:]:
            twin.canon_id = canon.id
            session.add(twin)
            auto_twins += 1
    session.flush()

    manual_changes = apply_manual_overrides(session)
    flatten_canon_chains(session)

    return AutoDedupResult(
        groups=out_groups,
        auto_twins=auto_twins,
        manual_changes=manual_changes,
    )
