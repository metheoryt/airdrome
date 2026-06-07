"""The `maint` group: rare housekeeping that isn't a pipeline stage.

Grouped by *kind* (maintenance), not by pipeline position, so it stays out of the top-level
flow. The reconcile roadmap will add siblings here (reconcile, recompute-main-files).
"""

import typer

from airdrome.console import done
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names

from .options import DRY_RUN
from .state import AppState


maint_app = typer.Typer(help="Maintenance tasks")


@maint_app.command()
def renormalize(ctx: typer.Context, dry_run: bool = DRY_RUN):
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
