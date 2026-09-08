"""Hard-conflict detection and automatic resolution for a multi-remote reconcile.

When one playlist is reconciled against several remotes in a run, the multiset 3-way
merge auto-resolves almost everything. The exception is an *order-dependent* edit: a
track one remote added (vs. its base) while another removed it (vs. its base). The final
membership then depends on which remote merges first, so the fold's arithmetic is not a
decision — `detect_conflicts` names those tracks and `resolve_latest` settles each one by
the last remote that actually edited it.
"""

from collections import Counter
from dataclasses import dataclass, field

from airdrome.enums import Source

from .sync import _three_way_merge


@dataclass(frozen=True)
class RemoteState:
    """One remote's view of a playlist for this run: its base and current membership.

    `base` is the per-(playlist, remote) snapshot from the last reconcile
    (`PlaylistLink.synced_track_ids`); `theirs` is the remote's current canonical
    membership. Both are ordered lists of canonical `Track.id`s, duplicates kept.
    """

    remote: Source
    base: list[int]
    theirs: list[int]


@dataclass
class PlaylistConflict:
    """A playlist and every remote's view of it, packaged for resolution.

    `states` are in reconcile order (sources first, then backends) so the fold runs the
    same way the orchestrator would apply them. `conflicts` is the set of canonical
    track ids `detect_conflicts(states)` flagged.
    """

    playlist_id: int
    playlist_name: str
    ours: list[int]
    states: list[RemoteState]
    conflicts: set[int] = field(default_factory=set)


def verdicts(conflict: PlaylistConflict) -> dict[int, tuple[Source, int]]:
    """Per conflicted track, the deciding remote and the multiplicity it wants.

    The decider is the *last remote in reconcile order that actually edited the track*.
    A remote whose `theirs` count equals its own base did not touch it and abstains
    rather than winning by position — otherwise a remote that merely reconciled last
    would silently overrule the only peer with an opinion. Also what `sync` reports.
    """
    decided: dict[int, tuple[Source, int]] = {}
    for st in conflict.states:  # reconcile order, so later editors overwrite earlier ones
        base_c, theirs_c = Counter(st.base), Counter(st.theirs)
        for track_id in sorted(conflict.conflicts):
            if theirs_c[track_id] != base_c[track_id]:
                decided[track_id] = (st.remote, theirs_c[track_id])
    return decided


def resolve_latest(conflict: PlaylistConflict) -> list[int]:
    """The canonical membership for one playlist, resolving conflicts without a human.

    Everything auto-merges: fold each remote in reconcile order, exactly as the pairwise
    engine would. Then, for the flagged tracks only, force the last editing remote's
    multiplicity — the fold's arithmetic (`ours + theirs - base`) is order-dependent for
    those and so is not a decision. Non-conflicting edits from every remote survive.

    Order follows reconcile decision #2: surplus copies are trimmed from the tail and
    missing ones appended at the end, so a settled playlist reconciles to itself.
    """
    merged = list(conflict.ours)
    for st in conflict.states:
        merged = _three_way_merge(st.base, merged, st.theirs)

    decided = verdicts(conflict)
    if not decided:
        return merged

    wanted = Counter(merged)  # what the fold settled on
    for track_id, (_, count) in decided.items():
        wanted[track_id] = count  # assignment, not Counter.update — the verdict replaces

    final: list[int] = []
    emitted: Counter[int] = Counter()
    for track_id in merged:  # keep position; drop the copies the verdict cut
        if emitted[track_id] < wanted[track_id]:
            final.append(track_id)
            emitted[track_id] += 1
    for track_id, (_, count) in decided.items():  # append what the verdict added
        while emitted[track_id] < count:
            final.append(track_id)
            emitted[track_id] += 1
    return final


def detect_conflicts(states: list[RemoteState]) -> set[int]:
    """Canonical track ids edited in opposing directions across the remotes.

    A track is *added* by a remote when its multiplicity in `theirs` exceeds the base,
    and *removed* when it falls below. A hard conflict is a track some remote added and
    some other remote removed — the only outcome that depends on reconcile order. Pure
    adds (or pure removes) from several remotes are deterministic and never conflict.
    """
    added: set[int] = set()
    removed: set[int] = set()
    for st in states:
        base_c, theirs_c = Counter(st.base), Counter(st.theirs)
        for track_id in set(base_c) | set(theirs_c):
            delta = theirs_c[track_id] - base_c[track_id]
            if delta > 0:
                added.add(track_id)
            elif delta < 0:
                removed.add(track_id)
    return added & removed
