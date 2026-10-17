"""The `maint` group: rare housekeeping that isn't a pipeline stage.

Grouped by *kind* (maintenance), not by pipeline position, so it stays out of the top-level
flow. The reconcile roadmap will add siblings here (reconcile, recompute-main-files).
"""

import typer

from airdrome.cloud.acoustid import AcoustIDKeyError, identify
from airdrome.conf import settings
from airdrome.console import console, done
from airdrome.library.enrich import reenrich
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names

from .options import DRY_RUN
from .state import AppState


maint_app = typer.Typer(help="Maintenance tasks", pretty_exceptions_show_locals=False)


@maint_app.command()
def renormalize(ctx: typer.Context, dry_run: bool = DRY_RUN) -> None:
    """Recompute the normalized `_norm` fields on tracks, aliases, and files.

    The escape hatch for a normalization-rule change: recompute in place instead of a full
    reimport.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run
    normalize_track_names(state.session)
    normalize_alias_names(state.session)
    normalize_track_file_names(state.session)
    done("Renormalized tracks, aliases, and files")


@maint_app.command()
def enrich(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Re-read every file, even if its hash is unchanged."),
    workers: int = typer.Option(4, "--workers", min=1, help="Parallel fpcalc processes."),
    dry_run: bool = DRY_RUN,
) -> None:
    """Re-read tags, ids, md5 and fingerprint of every existing file (evidence for identity binding).

    Writes only the database, so the session rollback under `--dry-run` is enough.
    """
    state: AppState = ctx.obj
    state.dry_run = dry_run
    stats = reenrich(state.session, force=force, workers=workers)
    console.print(f"recording id: {stats.with_mbid}/{stats.seen} files")
    console.print(f"fingerprint:  {stats.with_fp}/{stats.seen} files")
    console.print(
        f"updated {stats.updated}, missing/unreadable {stats.missing}, fingerprint failed {stats.fp_failed}"
    )
    done("Re-enriched existing files")


@maint_app.command()
def acoustid(
    ctx: typer.Context,
    limit: int | None = typer.Option(None, "--limit", min=1, help="Look up at most this many files."),
    dry_run: bool = DRY_RUN,
) -> None:
    """Look up fingerprinted files without a recording id on AcoustID; store confident matches.

    Needs `ACOUSTID_CLIENT_KEY`. Writes only the database, so the session rollback under
    `--dry-run` is enough; the network calls still happen.
    """
    key = settings.acoustid_client_key
    if not key:
        console.print("ACOUSTID_CLIENT_KEY is not set; register an application key at acoustid.org.")
        raise typer.Exit(1)
    state: AppState = ctx.obj
    state.dry_run = dry_run
    if dry_run:
        console.print("dry run: lookups still go to the network; nothing is saved")
    try:
        stats = identify(state.session, key, limit=limit)
    except AcoustIDKeyError as exc:
        console.print(f"{exc}; aborting.")
        raise typer.Exit(1) from None
    console.print(f"stored {stats.stored}/{stats.seen} recording ids, ambiguous {stats.ambiguous}")
    console.print(
        f"missing {stats.missing}, fingerprint failed {stats.fp_failed}, lookup failed {stats.failed}"
    )
    done("AcoustID lookup finished")
