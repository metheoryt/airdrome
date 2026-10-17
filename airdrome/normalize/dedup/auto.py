from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from airdrome.console import detail
from airdrome.enums import BindMethod
from airdrome.models import Track

from .grouping import CanonStrategy, canon_order, merge_overlapping_groups
from .persistence import (
    apply_manual_overrides,
    flatten_canon_chains,
    recompute_main_files,
    stored_cannot_links,
    stored_canons,
)
from .recording import scan_recordings


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


def _anchor_on_stored_canon(
    group: list[Track], governed: dict[int, int | None], rec_ids: set[int]
) -> list[Track]:
    """Put the stored canon first when recording identity brought a hand-made group's member here.

    The overrides run last and re-point every governed member to its stored canon; were the
    automatic canon a newcomer, that would strand it alone. Anchoring the group on the stored
    canon makes the newcomer join under it (spec, *manual veto*). Two limits:

    - Only a governed member that is itself in a recording group picks the root: the spec scopes
      the rule to newcomers that recording identity adds. A stored canon that only the flag sets
      reached (a member of an unrelated hand-made group) never re-canons a group, so a group
      without such a member keeps the plain canon order.
    - Only a stored canon already in `group`. It got there through the union-find (the
      stored-root edges in `auto_deduplicate`), so the cannot-link veto has already ruled on it;
      a root the veto kept out is never pulled in here.

    Should two stored groups meet in one merged group, the first governed member in canon order
    decides, and the overrides re-point the other group's members afterwards.
    """
    ids = {t.id for t in group}
    root_id = next(
        (r for t in group if t.id in rec_ids and t.id in governed and (r := governed[t.id] or t.id) in ids),
        None,
    )
    if root_id is None or group[0].id == root_id:
        return group
    root = next(t for t in group if t.id == root_id)
    return [root, *(t for t in group if t is not root)]


@dataclass
class AutoDedupResult:
    groups: list[list[Track]]
    auto_twins: int
    manual_changes: int
    conflicted: int = 0  # tracks left out of recording grouping: their files disagree on the id


def auto_deduplicate(
    session: Session,
    flag_sets: list[dict[str, bool]] | None = None,
    strategy: CanonStrategy = CanonStrategy.ADDED,
) -> AutoDedupResult:
    """Rebuild Track.canon_id from recording identity, flag-sets and stored manual overrides.

    Every run starts clean: all canon_ids (and their method/confidence) are
    reset. Recording identity (shared recording id, close fingerprint match —
    see `recording.py`) produces groups ahead of the flag-sets, each flag-set
    produces its own bucket-grouping, and all of them merge via union-find so
    a track ends up with one canon — except that a stored "not duplicates"
    choice is a hard, transitive veto on that union. Stored manual choices
    then layer on top (overriding any auto canon they touch), and a final pass
    flattens any canon chain the overlay may have introduced. `strategy` picks
    which member of each group becomes canon. Caller is responsible for
    committing.
    """
    if not flag_sets:
        flag_sets = [{}]

    # Reset before computing groups so the in-memory Track objects this
    # function returns reflect DB state (no stale canon_id values). The method
    # goes too: links `land` made are reproduced below, with the method of this run.
    session.execute(update(Track).values(canon_id=None, canon_method=None, canon_confidence=None))
    session.flush()

    rec = scan_recordings(session, strategy)
    for t in rec.conflicted:
        ids = ", ".join(sorted({f.recording_mbid for f in t.files if f.recording_mbid}))
        detail(f"  [yellow]conflicting recording ids[/yellow] {t.artist} — {t.title} ({t.album}): {ids}")
    rec_group_of = {t.id: j for j, (_, group) in enumerate(rec.auto) for t in group}

    raw: list[tuple[str, list[Track]]] = list(rec.auto)
    # A recording group's governed member pulls its stored canon in as an edge, so the canon is
    # joined through the same union-find, and so under the same cannot-link veto, as everything else.
    governed = stored_canons(session)
    for _, group in rec.auto:
        for t in group:
            root_id = governed.get(t.id)
            if root_id is not None:
                raw.append(("stored-canon", [t, session.get_one(Track, root_id)]))
    for i, flags in enumerate(flag_sets):
        groups = compute_auto_dedup_groups(session, strategy=strategy, **flags)
        excluded = sorted(k.removeprefix("with_") for k, v in flags.items() if v is False)
        key_suffix = "-" + "-".join(excluded) if excluded else "all"
        for j, group in enumerate(groups):
            raw.append((f"set{i}[{key_suffix}]#{j}", group))

    merged = merge_overlapping_groups(session, raw, strategy, cannot_link=stored_cannot_links(session))

    # Recording groups by member, so a twin's own recording evidence can name its link.
    rec_members = [{t.id for t in group} for _, group in rec.auto]

    auto_twins = 0
    out_groups: list[list[Track]] = []
    for _, group in merged:
        group = _anchor_on_stored_canon(group, governed, set(rec_group_of))
        out_groups.append(group)
        canon = group[0]
        ids = {t.id for t in group}
        for twin in group[1:]:
            twin.canon_id = canon.id
            # Recording evidence names the link when the twin's recording group reaches into this
            # group through another member; otherwise only the flag-sets joined it.
            rec_j = rec_group_of.get(twin.id)
            if rec_j is not None and len(rec_members[rec_j] & ids) > 1:
                twin.canon_method, twin.canon_confidence = rec.evidence[twin.id]
            else:
                twin.canon_method, twin.canon_confidence = BindMethod.DEDUP_FLAGS, None
            session.add(twin)
            auto_twins += 1
    session.flush()

    manual_changes = apply_manual_overrides(session)
    flatten_canon_chains(session)
    # The graph was fully rebuilt; re-pick each group's main across canon + twins.
    recompute_main_files(session)

    return AutoDedupResult(
        groups=out_groups,
        auto_twins=auto_twins,
        manual_changes=manual_changes,
        conflicted=len(rec.conflicted),
    )
