from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from airdrome.console import console
from airdrome.models import DedupGroup, DedupGroupMember, Track


if TYPE_CHECKING:
    from .manual import Page


def _load_stored_index(session: Session) -> dict[tuple[str, ...], DedupGroup]:
    """Map each stored group to the sorted multiset of its member hashes."""
    index: dict[tuple[str, ...], DedupGroup] = {}
    for group in session.scalars(select(DedupGroup)):
        hashes = tuple(sorted(m.member_hash for m in group.members))
        index[hashes] = group
    return index


def export_dedup_groups(session: Session) -> dict[str, dict]:
    """Serialize all stored dedup groups to the portable duplicates.json shape.

    Keyed by group label (decorative only); each value holds the members'
    `duplicate_hash` values and their parallel `canon_hash` picks. Members are
    sorted so output is deterministic. Re-import identity is the member-hash
    multiset, not the label, so a rare duplicate label is disambiguated here
    purely to avoid dropping a group from the dict.
    """
    out: dict[str, dict] = {}
    for group in session.scalars(select(DedupGroup).order_by(DedupGroup.id)):
        members = sorted(group.members, key=lambda m: m.member_hash)
        label = group.label or f"group-{group.id}"
        key, n = label, 2
        while key in out:
            key = f"{label}#{n}"
            n += 1
        out[key] = {
            "members": [m.member_hash for m in members],
            "canon_hashes": [m.canon_hash for m in members],
        }
    return out


def import_dedup_groups(session: Session, data: dict[str, dict]) -> tuple[int, int]:
    """Upsert dedup groups from the portable shape; identity = member-hash multiset.

    An entry whose member-hash multiset already matches a stored group replaces
    that group's members/canon picks; otherwise a new group is created. Returns
    (created, updated). Caller is responsible for committing.
    """
    index = _load_stored_index(session)
    created = updated = 0
    for label, entry in data.items():
        members, canons = entry["members"], entry["canon_hashes"]
        key = tuple(sorted(members))
        existing = index.get(key)
        group = existing if existing is not None else DedupGroup()
        group.label = label
        group.members = [
            DedupGroupMember(member_hash=m, canon_hash=c) for m, c in zip(members, canons, strict=True)
        ]
        session.add(group)
        if existing is not None:
            updated += 1
        else:
            created += 1
            index[key] = group  # collapse duplicate entries within the same file
    session.flush()
    return created, updated


def save_confirmed_groups(session: Session, pages: dict[str, Page]) -> None:
    """Persist confirmed picks to the DB, keyed by member-hash multiset.

    Only pages materialized in this run are touched: a confirmed page is
    upserted; a materialized page that matches a stored group but is no
    longer confirmed (user reset it) is deleted. Stored groups not present
    in this run are left untouched.
    """
    index = _load_stored_index(session)
    n_saved = 0
    for key, page in pages.items():
        hashes = tuple(sorted(t.duplicate_hash for t in page.tracks))
        existing = index.get(hashes)
        if not page.confirmed:
            if existing is not None:
                session.delete(existing)
            continue
        id_to_hash = {t.id: t.duplicate_hash for t in page.tracks}
        canon_by_member = {
            t.duplicate_hash: (id_to_hash.get(canon_id) if canon_id is not None else None)
            for t, canon_id in zip(page.tracks, page.chosen_canons, strict=False)
        }
        group = existing if existing is not None else DedupGroup()
        group.label = key
        group.members = [
            DedupGroupMember(member_hash=h, canon_hash=canon_by_member.get(h))
            for h in (t.duplicate_hash for t in page.tracks)
        ]
        session.add(group)
        n_saved += 1
    session.flush()
    console.print(f"[dim]Saved {n_saved} confirmed group(s) to DB[/dim]")


def load_confirmed_groups(session: Session, pages: dict[str, Page]) -> None:
    """Restore confirmed picks onto the given pages from stored DedupGroup rows.

    For each page whose member-hash multiset matches a stored group, set
    `chosen_canons` to the stored canon picks and mark the page confirmed.
    Pages without a matching stored group are left as-is.
    """
    index = _load_stored_index(session)
    n_restored = 0
    for page in pages.values():
        hashes = tuple(sorted(t.duplicate_hash for t in page.tracks))
        stored = index.get(hashes)
        if stored is None:
            continue
        saved_canon_by_member = {m.member_hash: m.canon_hash for m in stored.members}
        hash_to_id = {t.duplicate_hash: t.id for t in page.tracks}
        restored: list[int | None] = []
        for t in page.tracks:
            canon_hash = saved_canon_by_member.get(t.duplicate_hash)
            if canon_hash is None:
                restored.append(None)
            else:
                resolved = hash_to_id.get(canon_hash)
                if resolved is None:
                    break
                restored.append(resolved)
        else:
            page.chosen_canons = restored
            page.confirmed = True
            page.auto_resolved = False  # human-confirmed overrides auto-resolved status
            n_restored += 1
    console.print(f"[dim]Loaded {n_restored}/{len(index)} group(s) from DB[/dim]")


def apply_manual_overrides(session: Session) -> int:
    """Apply stored manual dedup choices onto Track.canon_id.

    A stored group governs its tracks entirely: for every group still fully
    present in the library (same member-hash multiset), each member's
    canon_id is set to the stored choice (or cleared), overriding any auto
    canon. Caller is responsible for committing.
    """
    index = _load_stored_index(session)
    if not index:
        return 0

    by_hash: dict[str, list[Track]] = {}
    for t in session.scalars(select(Track)):
        by_hash.setdefault(t.duplicate_hash, []).append(t)

    changed = 0
    for hashes, group in index.items():
        present = tuple(sorted(h for h in hashes if h in by_hash))
        # Require the whole group to still be present so we never apply a
        # partial, possibly-misleading override.
        if present != hashes:
            continue
        canon_by_member = {m.member_hash: m.canon_hash for m in group.members}
        for member_hash, tracks in by_hash.items():
            if member_hash not in canon_by_member:
                continue
            canon_hash = canon_by_member[member_hash]
            canon_id = None
            if canon_hash is not None:
                canon_tracks = by_hash.get(canon_hash)
                if not canon_tracks:
                    continue
                canon_id = canon_tracks[0].id
            for t in tracks:
                new_canon = None if canon_id == t.id else canon_id
                if t.canon_id != new_canon:
                    t.canon_id = new_canon
                    session.add(t)
                    changed += 1
    session.flush()
    return changed


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
