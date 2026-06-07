import json
from pathlib import Path

import typer

from airdrome.conf import settings
from airdrome.console import console, done
from airdrome.library.organize import organize_library
from airdrome.normalize.dedup import (
    FIELDS,
    RECOMMENDED_SETS,
    CanonStrategy,
    Deduplicator,
    DeduplicatorUI,
    auto_deduplicate,
    export_dedup_groups,
    flag_set,
    import_dedup_groups,
)
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names

from .options import DRY_RUN
from .state import AppState


library_app = typer.Typer(help="Library tools")


@library_app.command("organize")
def library_organize(
    ctx: typer.Context,
    move: bool = typer.Option(
        False, "--move", "-m", help="Move files into LIBRARY_DIR instead of copying them."
    ),
    dry_run: bool = DRY_RUN,
):
    """Copy (or --move) bound files into LIBRARY_DIR, picking the best copy as each track's main."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    organize_library(state.session, dst_dir=settings.library_dir, copy=not move)


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
_CANON_HELP = "Which member of each group becomes canon: 'added' (earliest added) or 'year' (oldest release)."


@library_app.command("deduplicate")
def deduplicate_cli(
    ctx: typer.Context,
    sets: list[str] = typer.Option(
        None,
        "--set",
        "-s",
        help=f"{_SET_HELP} Defaults to three single-field sets (artist/album_artist/album).",
    ),
    canon: CanonStrategy = typer.Option(CanonStrategy.ADDED, "--canon", "-c", help=_CANON_HELP),
    match: str = typer.Option("", "--match", help="Filter by a substring"),
):
    """Interactively review duplicate groups and pick canons (TUI)."""
    state: AppState = ctx.obj
    flag_sets = [_parse_set(s) for s in sets] if sets else None
    Deduplicator(state.session, flag_sets=flag_sets, strategy=canon, partial_match=match).run()


@library_app.command("auto-deduplicate")
def auto_deduplicate_cli(
    ctx: typer.Context,
    sets: list[str] = typer.Option(
        None,
        "--set",
        "-s",
        help=f"{_SET_HELP} No --set uses the recommended sets "
        '("artist,duration" + "artist,year" + "album_artist,duration").',
    ),
    canon: CanonStrategy = typer.Option(CanonStrategy.ADDED, "--canon", "-c", help=_CANON_HELP),
):
    """Rebuild Track.canon_id from N flag-sets + stored manual overrides.

    Every run is a clean slate: all canon_ids are reset, each --set produces
    its own bucket-grouping, overlapping groups across sets are merged, then
    stored manual choices layer on top and any canon chain is flattened.
    With no --set, the recommended sets are used (see RECOMMENDED_SETS).
    """
    state: AppState = ctx.obj

    flag_sets = [_parse_set(s) for s in sets] if sets else RECOMMENDED_SETS
    result = auto_deduplicate(state.session, flag_sets=flag_sets, strategy=canon)

    for group in result.groups:
        canons = [None] + [group[0].id] * (len(group) - 1)
        console.print(DeduplicatorUI.compose_table("auto-dedup", group, canons))

    done(
        f"{result.auto_twins} twin(s) across {len(result.groups)} group(s)"
        f" + {result.manual_changes} manual override(s) from stored choices"
    )


@library_app.command("export-duplicates")
def export_duplicates_cli(
    ctx: typer.Context,
    path: Path = typer.Argument(None, help="Output JSON file (default: DUPLICATES_FILEPATH)."),
):
    """Dump confirmed dedup groups from the DB to a portable JSON file."""
    state: AppState = ctx.obj
    dest = path or settings.duplicates_filepath
    data = export_dedup_groups(state.session)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    done(f"Exported {len(data)} group(s) to {dest}")


@library_app.command("import-duplicates")
def import_duplicates_cli(
    ctx: typer.Context,
    path: Path = typer.Argument(None, help="Input JSON file (default: DUPLICATES_FILEPATH)."),
    dry_run: bool = DRY_RUN,
):
    """Load confirmed dedup groups from a JSON file into the DB (idempotent)."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    src = path or settings.duplicates_filepath
    if not src.exists():
        console.print(f"[red]No such file: {src}[/red]")
        raise typer.Exit(1)
    data = json.loads(src.read_text(encoding="utf-8"))
    created, updated = import_dedup_groups(state.session, data)
    done(f"Imported {created} new + {updated} updated group(s) from {src}")


@library_app.command()
def renormalize(ctx: typer.Context, dry_run: bool = DRY_RUN):
    """Recompute the normalized `_norm` fields on tracks, aliases, and files."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    normalize_track_names(state.session)
    normalize_alias_names(state.session)
    normalize_track_file_names(state.session)
    done("Renormalized tracks, aliases, and files")
