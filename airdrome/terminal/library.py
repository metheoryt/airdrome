import typer

from airdrome.conf import settings
from airdrome.console import console
from airdrome.library.organize import organize_library
from airdrome.normalize.dedup import Deduplicator, DeduplicatorUI, auto_deduplicate
from airdrome.normalize.names import normalize_alias_names, normalize_track_file_names, normalize_track_names

from .state import AppState


library_app = typer.Typer(help="Library tools")

_DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")


@library_app.command("organize")
def library_organize(
    ctx: typer.Context,
    copy: bool = typer.Option(False, "--copy", "-c"),
    reset: bool = typer.Option(False, "--reset", "-r"),
    dry_run: bool = _DRY_RUN,
):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    organize_library(state.session, dst_dir=settings.library_dir, copy=copy, reset=reset)


@library_app.command("deduplicate")
def deduplicate_cli(
    ctx: typer.Context,
    match: str = typer.Option("", "--match", help="Filter by a substring"),
):
    state: AppState = ctx.obj
    Deduplicator(state.session, partial_match=match).run()


_VALID_FIELDS = {"artist", "album_artist", "album", "track_n", "disc_n", "duration", "year"}


def _parse_set(spec: str) -> dict[str, bool]:
    fields = {f.strip() for f in spec.split(",") if f.strip()}
    unknown = fields - _VALID_FIELDS
    if unknown:
        raise typer.BadParameter(
            f"Unknown field(s): {', '.join(sorted(unknown))}. Valid: {', '.join(sorted(_VALID_FIELDS))}"
        )
    return {f"with_{f}": (f in fields) for f in _VALID_FIELDS}


@library_app.command("auto-deduplicate")
def auto_deduplicate_cli(
    ctx: typer.Context,
    sets: list[str] = typer.Option(
        None,
        "--set",
        "-s",
        help=(
            'Flag-set as comma-separated fields (repeatable). Example: --set "artist,album,year". '
            "Listed fields are included; title is always implicit. No --set means one set with all "
            "fields on. Multiple --sets union-find-merge their groups."
        ),
    ),
):
    """Rebuild Track.canon_id from N flag-sets + stored manual overrides.

    Every run is a clean slate: all canon_ids are reset, each --set produces
    its own bucket-grouping, overlapping groups across sets are merged, then
    stored manual choices layer on top and any canon chain is flattened.
    """
    state: AppState = ctx.obj

    flag_sets = [_parse_set(s) for s in sets] if sets else None
    result = auto_deduplicate(state.session, flag_sets=flag_sets)

    for group in result.groups:
        canons = [None] + [group[0].id] * (len(group) - 1)
        console.print(DeduplicatorUI.compose_table("auto-dedup", group, canons))

    console.print(
        f"[green]{result.auto_twins} twin(s) across {len(result.groups)} group(s)"
        f" + {result.manual_changes} manual override(s) from stored choices.[/green]"
    )


@library_app.command()
def renormalize(ctx: typer.Context, dry_run: bool = _DRY_RUN):
    state: AppState = ctx.obj
    state.dry_run = dry_run
    normalize_track_names(state.session)
    normalize_alias_names(state.session)
    normalize_track_file_names(state.session)
