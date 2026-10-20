"""Durable mirror of the manual dedup decisions, colocated with the library.

Manual decisions (pair decisions and canon choices) are real human work, but the Postgres DB is
disposable — it is recreated on any schema change. So the DB is the working copy and this JSON file is
the durable mirror: one-directional (DB → file), rewritten in full (a version 2 document, see
`decisions.dump_v2`) after every commit that touched a decision, and read back only into empty tables.

**The snapshot and the write are deliberately split.** `stash_mirror_snapshot` (in `store`) exports
the tables inside the writing transaction, where the pending inserts and updates are already visible;
the `after_commit` listener installed here only serializes that dict to disk. Exporting in the listener
instead would emit SQL with no transaction open — SQLAlchemy would silently start one that nothing
closes — and writing before the commit would let a rollback leave the file ahead of the DB.

The mirror is always the *whole* tables, never a delta, and empty tables write the empty v2 document
rather than skipping: a stale file left behind would re-seed the next empty DB.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.models import CanonChoice, PairDecision

from .decisions import is_legacy, load_v2
from .store import MIRROR_SNAPSHOT_KEY, import_decisions


def write_mirror(data: dict[str, object], path: Path) -> None:
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
    """Keep `path` in step with every commit that changed a decision."""

    @event.listens_for(session, "after_commit")
    def _write_snapshot(sess: Session) -> None:
        data = sess.info.pop(MIRROR_SNAPSHOT_KEY, None)
        if data is None:  # this commit touched no decision
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
    """Seed empty decision tables from the mirror; returns the rows restored (pairs + canons).

    Only *empty* tables are seeded, which is what keeps this from fighting the DB; the case this exists
    for is tables wiped from outside the app — a schema rebuild. A missing file or an empty `{}` is a
    no-op. Caller commits.

    Loud on anything else it cannot restore faithfully: an unparsable file raises, and so does the old
    version-less group format. Importing nothing from that one would let the next commit overwrite the
    old groups with an empty v2 document, so the file is left untouched and the owner converts it.
    """
    for table in (PairDecision, CanonChoice):
        if session.scalar(select(table.id).limit(1)) is not None:
            return 0
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if data == {}:
        return 0
    if is_legacy(data):
        raise ValueError(
            f"{path} holds dedup groups in the old version-less format; it was not restored and is left "
            "untouched. Convert them from the OLD database, before recreating it: "
            "`uv run python scripts/migrate_dedup_groups.py OUT`, then move OUT to this path. If the "
            "database is already recreated, restore the old one from its dump (pg_dump) and convert from "
            "that. `airdrome dedup-import <file>` is only for an archived legacy file elsewhere, "
            "never this one"
        )
    pairs, canons = load_v2(data)
    counts = import_decisions(session, pairs, canons)
    if counts.created:
        console.print(f"[dim]Restored {counts.created} dedup decision(s) from {path}[/dim]")
    return counts.created
