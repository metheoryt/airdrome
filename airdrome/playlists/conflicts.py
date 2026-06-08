"""Hard-conflict detection for a multi-remote reconcile.

When one playlist is reconciled against several remotes in a run, the multiset 3-way
merge auto-resolves almost everything. The exception is an *order-dependent* edit: a
track one remote added (vs. its base) while another removed it (vs. its base). The final
membership then depends on which remote merges first, so the engine must not guess — it
surfaces these tracks for interactive resolution instead.
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

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


class Strategy(StrEnum):
    """How the user resolves one conflicted playlist."""

    AUTO = "auto"  # sequential 3-way merge across the remotes
    OURS = "ours"  # keep canonical as-is, ignore the remotes' edits this run
    TAKE = "take"  # one remote wins wholesale (needs Decision.remote)


@dataclass(frozen=True)
class Decision:
    strategy: Strategy
    remote: Source | None = None  # set iff strategy is TAKE


@dataclass
class PlaylistConflict:
    """A playlist whose remotes disagree, packaged for the resolver.

    `states` are in reconcile order (sources first, then backends) so AUTO folds the
    same way the orchestrator would apply them. `conflicts` is the set of canonical
    track ids `detect_conflicts(states)` flagged.
    """

    playlist_id: int
    playlist_name: str
    ours: list[int]
    states: list[RemoteState]
    conflicts: set[int] = field(default_factory=set)


def resolve_final(conflict: PlaylistConflict, decision: Decision) -> list[int]:
    """The canonical membership a chosen strategy yields for a conflicted playlist."""
    if decision.strategy is Strategy.OURS:
        return list(conflict.ours)
    if decision.strategy is Strategy.TAKE:
        for st in conflict.states:
            if st.remote == decision.remote:
                return list(st.theirs)
        raise ValueError(f"remote {decision.remote} is not part of this conflict")
    merged = list(conflict.ours)
    for st in conflict.states:  # AUTO: fold in reconcile order, matching the engine
        merged = _three_way_merge(st.base, merged, st.theirs)
    return merged


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
