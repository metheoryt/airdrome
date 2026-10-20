"""Top-level pipeline commands: organize, dedup, and the dedup JSON round-trip.

These are stages of the canonical workflow (`import → land → organize → dedup`), so they live
at the top level rather than under a group. They attach to the root app via `register(app)` to
keep `app.py` focused on session lifecycle and the import/land stages.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.markup import escape

from airdrome.conf import settings
from airdrome.console import console, done
from airdrome.library.organize import organize_library
from airdrome.normalize.dedup import (
    FIELDS,
    RECOMMENDED_SETS,
    CanonStrategy,
    auto_deduplicate,
    flag_set,
    import_decisions,
    import_legacy,
    restore_if_empty,
)
from airdrome.normalize.dedup.decisions import is_legacy, load_v2

from .options import DRY_RUN
from .state import AppState
from .tables import dedup_table


def _parse_set(spec: str) -> dict[str, bool]:
    """Parse a comma-separated `--set` spec into a compute flag-set."""
    fields = {f.strip() for f in spec.split(",") if f.strip()}
    unknown = fields - set(FIELDS)
    if unknown:
        raise typer.BadParameter(
            f"Unknown field(s): {', '.join(sorted(unknown))}. Valid: {', '.join(sorted(FIELDS))}"
        )
    return flag_set(*fields)


_SET_HELP = (
    'Flag-set as comma-separated fields (repeatable). Example: --set "artist,album,year". '
    "Listed fields are included; title is always implicit. Multiple --sets union-find-merge "
    "their groups."
)
_CANON_HELP = (
    "Which member of each group becomes canon: 'added' (earliest added) or 'year' (oldest release). "
    "Applied after the compilation rule, which leads for every group: the original album wins unless "
    "you own >= 2 tracks of the compilation."
)


def organize(
    ctx: typer.Context,
    move: bool = typer.Option(
        False, "--move", "-m", help="Move files into LIBRARY_DIR instead of copying them."
    ),
    # Not the shared DRY_RUN: this one also skips the filesystem write, it does not undo it.
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Report what would be transferred; write nothing."
    ),
) -> None:
    """Copy (or --move) bound files into LIBRARY_DIR, picking the best copy as each track's main."""
    state: AppState = ctx.obj
    state.dry_run = dry_run  # rolls the DB back on close; `dry_run` below spares the disk
    organize_library(state.session, dst_dir=settings.library_dir, copy=not move, dry_run=dry_run)


def dedup(
    ctx: typer.Context,
    sets: list[str] = typer.Option(
        None,
        "--set",
        "-s",
        help=f"{_SET_HELP} No --set uses the recommended sets "
        '("artist,duration" + "artist,year" + "album_artist,duration").',
    ),
    canon: CanonStrategy = typer.Option(CanonStrategy.ADDED, "--canon", "-c", help=_CANON_HELP),
    dry_run: bool = DRY_RUN,
) -> None:
    """Rebuild Track.canon_id from the manual decisions + recording identity + N flag-sets.

    The batch pass is a clean slate: all canon_ids are reset, the manual "same" decisions form
    groups first, recording identity (shared recording id, close fingerprint match) and each --set
    add their own, and overlapping groups are merged — never across a manual "different" decision.
    A manual canon choice picks a group's canon; otherwise --canon does. With no --set the
    recommended sets are used.

    Manual decisions are made through the MCP tools, not here.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run  # writes only the DB (the mirror follows the commit), so the rollback is enough
    # A rebuilt DB has no decisions; seed them from the mirror before the batch pass
    # reads them, or a schema rebuild would silently drop every manual decision.
    try:
        restore_if_empty(state.session, settings.duplicates_file)
    except ValueError as exc:  # an old-format or unreadable mirror: nothing was restored or written
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    flag_sets = [_parse_set(s) for s in sets] if sets else RECOMMENDED_SETS
    result = auto_deduplicate(state.session, flag_sets=flag_sets, strategy=canon)

    for group in result.groups:
        canons = [None] + [group[0].id] * (len(group) - 1)
        console.print(dedup_table("auto-dedup", group, canons))

    conflicted = (
        f"; {result.conflicted} track(s) with conflicting recording ids left out (-v lists them)"
        if result.conflicted
        else ""
    )
    parts = [
        f"{result.auto_twins} automatic + {result.manual_links} manual twin(s)"
        f" across {len(result.groups)} group(s)"
    ]
    if result.superseded:
        parts.append(f"{result.superseded} superseded decision(s)")
    if result.dead:
        parts.append(f"{result.dead} dead decision(s) (naming a track no longer in the library)")
    done("; ".join(parts) + conflicted)


def dedup_import(
    ctx: typer.Context,
    path: Path = typer.Argument(None, help="Input JSON file (default: the library's mirror)."),
    dry_run: bool = DRY_RUN,
) -> None:
    """Load manual dedup decisions from a JSON file into the DB (idempotent; newest wins row by row).

    The library's own mirror is restored automatically by `dedup`, so this is the manual entrance:
    seeding one library from another's file, or reviving an old backup. A version 2 file keeps its
    timestamps, so a decision newer in the DB is kept. An old version-less group file is converted
    with the migration's conversion rules, each group decided now, in file order — except the live
    mirror itself, which is refused (convert it from the old DB with scripts/migrate_dedup_groups.py).
    An import that created or replaced a decision overwrites this library's mirror on commit, by
    design — what you import becomes the durable copy.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run
    src = path or settings.duplicates_file
    if not src.exists():
        console.print(f"[red]No such file: {escape(str(src))}[/red]")
        raise typer.Exit(1)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        if is_legacy(data) and src.resolve() == settings.duplicates_file.resolve():
            # Importing the live old-format mirror onto itself loses decisions: before `land` every member
            # is dropped and the file is replaced by an empty document; after it, the file's label order
            # (not the old DB's group order) decides conflicts. Its source of truth is the old DB.
            raise ValueError(
                f"{src} is this library's mirror in the old version-less format; it is not imported over "
                "itself. Convert it from the old database (before recreating it) with "
                "`uv run python scripts/migrate_dedup_groups.py OUT`, then move OUT to the mirror path"
            )
        dropped = ""
        if is_legacy(data):
            counts, converted = import_legacy(state.session, data, datetime.now(UTC))
            dropped = f"; {converted.dropped} member(s) dropped (no live track)"
        else:
            counts = import_decisions(state.session, *load_v2(data))
    except ValueError as exc:  # unparsable JSON, a malformed document, a pair listed twice, the live mirror
        state.session.rollback()  # the close hook still commits: make sure nothing half-imported lands
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    done(
        f"Imported {counts.created} new + {counts.replaced} replaced"
        f" ({counts.kept} kept (equal or newer in DB)) decision(s) from {escape(str(src))}{dropped}"
    )


def register(app: typer.Typer) -> None:
    """Attach the pipeline commands to the root app (they are top-level, not a group)."""
    app.command("organize")(organize)
    app.command("dedup")(dedup)
    app.command("dedup-import")(dedup_import)
