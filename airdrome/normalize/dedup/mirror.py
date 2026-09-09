"""Durable mirror of confirmed dedup groups, colocated with the library.

Confirmed dedup groups are real human work, but the Postgres DB is disposable —
it is recreated on any schema change. So the DB is the working copy and this JSON
file is the durable mirror: one-directional (DB → file), rewritten in full after
every commit that touched a group, and read back only into an empty table.

**The snapshot and the write are deliberately split.** `stash_mirror_snapshot`
(in `persistence`) exports the table inside the writing transaction, where the
pending inserts and deletes are already visible; the `after_commit` listener
installed here only serializes that dict to disk. Exporting in the listener
instead would emit SQL with no transaction open — SQLAlchemy would silently start
one that nothing closes — and writing before the commit would let a rollback
leave the file ahead of the DB.

The mirror is always the *whole* table, never a delta: `save_confirmed_groups`
touches only the pages materialized in its run, so nothing but the table itself
is authoritative. A group whose hashes no longer match any materialized page is
therefore preserved indefinitely — the same thing the DB does today.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.models import DedupGroup

from .persistence import MIRROR_SNAPSHOT_KEY, import_dedup_groups


def write_mirror(data: dict[str, dict], path: Path) -> None:
    """Replace `path` with `data`, atomically.

    The temp file goes in the *target* directory on purpose: `os.replace` is only
    atomic within one filesystem, and the system temp dir is usually another one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".duplicates-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install_mirror(session: Session, path: Path) -> None:
    """Keep `path` in step with every commit that changed a dedup group."""

    @event.listens_for(session, "after_commit")
    def _write_snapshot(sess: Session) -> None:
        data = sess.info.pop(MIRROR_SNAPSHOT_KEY, None)
        if data is None:  # this commit touched no group
            return
        try:
            write_mirror(data, path)
        except OSError as exc:
            # The DB commit already landed and the next one rewrites the file, so a
            # failed mirror write is recoverable — warn rather than unwind.
            console.print(f"[yellow]Could not write dedup mirror {path}: {exc}[/yellow]")

    @event.listens_for(session, "after_soft_rollback")
    def _drop_snapshot(sess: Session, previous_transaction: object) -> None:
        # Otherwise a snapshot that never landed would be written by the next
        # unrelated commit, putting the file ahead of the DB.
        sess.info.pop(MIRROR_SNAPSHOT_KEY, None)


def restore_if_empty(session: Session, path: Path) -> int:
    """Seed an empty group table from the mirror; returns how many groups landed.

    Only an *empty* table is seeded, which is what keeps this from fighting the DB:
    a group the user reset in the TUI stays gone, because that reset emptied the
    mirror too. The case this exists for is a table wiped from outside the app —
    a schema rebuild. Caller commits.

    A mirror that fails to parse raises: an unreadable durable copy of human work
    should be loud, not silently treated as absent.
    """
    if session.scalar(select(DedupGroup.id).limit(1)) is not None:
        return 0
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data:
        return 0
    created, _ = import_dedup_groups(session, data)
    if created:
        console.print(f"[dim]Restored {created} dedup group(s) from {path}[/dim]")
    return created
