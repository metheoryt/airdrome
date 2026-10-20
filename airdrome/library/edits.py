"""Point edits on the canon graph: "same track" / "different tracks", and manual alias binds.

A same/different edit stores one pair decision (`origin=mcp`), then replans the library with the CLI
defaults and applies the plan to the tracks the edit can reach (`edit_scope`), so its outcome is exactly
what the next full `dedup` run produces for them: the resolver is the single source of truth. Domain
functions only flush; the caller commits.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from airdrome.enums import BindMethod, DecisionKind, DecisionOrigin
from airdrome.models import PairDecision, Track, TrackAlias, TrackAliasScrobble, TrackPlay
from airdrome.normalize.dedup.auto import apply_plan, edit_scope, plan_dedup
from airdrome.normalize.dedup.decisions import key_text, pair, track_key
from airdrome.normalize.dedup.grouping import RECOMMENDED_SETS, CanonStrategy, canon_order
from airdrome.normalize.dedup.persistence import recompute_main_files
from airdrome.normalize.dedup.store import stash_mirror_snapshot, upsert_pair
from airdrome.scrobbles.copy_plays import copy_alias_plays


class EditError(ValueError):
    """An edit that cannot be applied: an unknown id or a self-pair."""


def _get(s: Session, track_id: int) -> Track:
    """Load one track or raise `EditError`."""
    t = s.get(Track, track_id)
    if t is None:
        raise EditError(f"unknown track id {track_id}")
    return t


def group_of(s: Session, track_id: int) -> list[Track]:
    """The canon and all its twins, canon first (twins in canon order)."""
    t = _get(s, track_id)
    canon = t.canon or t  # canon_id is terminal: one hop reaches the root
    twins = s.scalars(select(Track).where(Track.canon_id == canon.id).order_by(*canon_order()))
    return [canon, *twins]


def _check_pair(s: Session, a_id: int, b_id: int) -> tuple[Track, Track]:
    """Validate an edit's two ids and load their tracks."""
    if a_id == b_id:
        raise EditError(f"track {a_id} paired with itself")
    return _get(s, a_id), _get(s, b_id)


def _decide(s: Session, a: Track, b: Track, kind: DecisionKind) -> None:
    """Store the owner's decision on (a, b), replan, apply it to the reachable tracks and stash the mirror."""
    p = pair(key_text(track_key(a)), key_text(track_key(b)), kind, datetime.now(UTC), DecisionOrigin.MCP)
    stored = s.scalars(select(PairDecision).filter_by(key_a=p.a, key_b=p.b)).one_or_none()
    if stored is not None and stored.decided_at >= p.decided_at:
        # A stored row at or after "now" (a file from a box whose clock ran fast, or two edits within one
        # clock tick) would make the upsert keep it, silently dropping this edit. The edit is the newest
        # decision by definition, so it goes just after the stored one.
        p = pair(p.a, p.b, kind, stored.decided_at + timedelta(microseconds=1), DecisionOrigin.MCP)
    upsert_pair(s, p)

    plan = plan_dedup(s, RECOMMENDED_SETS, CanonStrategy.ADDED)
    touched = apply_plan(s, plan, edit_scope(s, plan, {a.id, b.id}))
    # Only the touched groups: a library-wide walk would re-flag unrelated groups.
    recompute_main_files(s, touched)
    stash_mirror_snapshot(s)


def mark_same(s: Session, a_id: int, b_id: int) -> Track:
    """Record that `a_id` and `b_id` are the same track; returns the canon of the group `a_id` ends in."""
    a, b = _check_pair(s, a_id, b_id)
    _decide(s, a, b, DecisionKind.SAME)
    return a.canon or a


def mark_different(s: Session, a_id: int, b_id: int) -> None:
    """Record that `a_id` and `b_id` are different tracks; the resolution decides who leaves which group."""
    a, b = _check_pair(s, a_id, b_id)
    _decide(s, a, b, DecisionKind.DIFFERENT)


def get_aliases(s: Session, alias_ids: list[int]) -> list[TrackAlias]:
    """Load every alias in `alias_ids`, or raise `EditError` naming the unknown ids."""
    found = {a.id: a for a in s.scalars(select(TrackAlias).where(TrackAlias.id.in_(alias_ids)))}
    missing = sorted(set(alias_ids) - found.keys())
    if missing:
        raise EditError(f"unknown alias id(s) {missing}")
    return [found[i] for i in dict.fromkeys(alias_ids)]


def bind_aliases(s: Session, alias_ids: list[int], track_id: int) -> Track:
    """Bind aliases to the canon of `track_id` by hand and sync their plays; returns that canon."""
    track = _get(s, track_id)
    canon = track.canon or track  # canon_id is terminal: one hop reaches the root
    aliases = get_aliases(s, alias_ids)
    for alias in aliases:
        alias.track = canon
        alias.match_method = BindMethod.MANUAL
        alias.match_confidence = 1.0
    s.flush()
    for alias in aliases:
        # Upserts by scrobble id, so plays already on the alias's old track are re-pointed, not duplicated.
        copy_alias_plays(s, alias)
    s.flush()
    return canon


def reject_aliases(s: Session, alias_ids: list[int]) -> None:
    """Unbind aliases and mark them rejected (NULL track + MANUAL), so matching never re-binds them."""
    aliases = get_aliases(s, alias_ids)
    for alias in aliases:
        # Not `alias.track = None`: `Track.aliases` is delete-orphan, so detaching would delete the alias.
        alias.track_id = None
        alias.match_method = BindMethod.MANUAL
        alias.match_confidence = None
    s.flush()
    for alias in aliases:
        s.expire(alias, ["track"])  # the loaded relationship still names the old track
    scrobble_ids = select(TrackAliasScrobble.id).where(
        TrackAliasScrobble.alias_id.in_([a.id for a in aliases])
    )
    s.execute(delete(TrackPlay).where(TrackPlay.source_scrobble_id.in_(scrobble_ids)))
    s.flush()
