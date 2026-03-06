from collections import defaultdict

from sqlmodel import Session, select, func

from jellyfist.conf import settings
from jellyfist.models import Track
from .schemas import DupGroup
from rich.table import Table
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.progress import Progress, TextColumn, BarColumn, MofNCompleteColumn

DUPES: dict[str, list[DupGroup]] = DupGroup.load(settings.duplicates_filepath)

console = Console()
progress = Progress(
    TextColumn("[bold blue]{task.description}"),
    BarColumn(),
    MofNCompleteColumn(),
)


def get_table_rows(t: Track) -> list[list[str]]:
    rows = []
    base_row = [
        str(t.id),
        t.title,
        t.artist or "",
        t.album_artist or "",
        t.album or "",
        str(t.track_n) if t.track_n is not None else "",
        str(t.disc_n) if t.disc_n is not None else "",
        str(len(t.files)) if len(t.files) else "",
    ]

    if not t.apple_tracks:
        rows.append(base_row)
        return rows

    for at in t.apple_tracks:
        tt = None
        if at.total_time:
            secs = at.total_time // 1000
            tt = f"{secs // 60}:{secs % 60:02d}"
        row = base_row.copy()
        row.extend(
            [
                "cloud" if at.apple_music else "local",
                str(at.apple_track_id),
                str(at.year),
                tt,
                f"{at.size / 1024 / 1024:.2f} MB",
                f"{at.bit_rate} kbps",
                at.date_added.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
        rows.append(row)
    return rows


def compose_table(key: str, tracks: list[Track]):
    table = Table(title=f"Duplicates by {key}")
    table.add_column("ID", style="blue")
    table.add_column("Title", style="orange4")
    table.add_column("Artist", style="magenta")
    table.add_column("Album Artist", style="green")
    table.add_column("Album", style="yellow")
    table.add_column("Track №", style="pink3")
    table.add_column("Disc №", style="pink3")
    table.add_column("Files", style="yellow")

    if any(len(t.apple_tracks) for t in tracks):
        # at least 1 track has apple data, add corresponding columns
        table.add_column("Apple Cloud", style="red")
        table.add_column("Apple Track ID", style="red")
        table.add_column("Year", style="red")
        table.add_column("Time", style="red")
        table.add_column("Size", style="red")
        table.add_column("Bit Rate", style="red")
        table.add_column("Added", style="red")

    for i, t in enumerate(tracks):
        rows = get_table_rows(t)
        row_kw = {}
        if t.twins:
            row_kw["style"] = "bold"
        if t.canon_id:
            row_kw["style"] = "dim"

        for row in rows:
            table.add_row(*row, **row_kw)

    return table


def prompt_duplicate_group(key: str, tracks: list[Track]) -> list[DupGroup]:
    # canon_idx: set[twin_idx]
    twins = defaultdict(set)

    # available ids
    ids = {t.id for t in tracks}

    # ids selected as twins (to check against)
    occupied_twin_ids = set()

    # populate keys with current canons
    # for i, t in enumerate(tracks):
    #     if t.twins:
    #         twins[t.id].update(set(tw.id for tw in t.twins))
    #         occupied_twin_ids.update(twins[t.id])
    #     if t.canon:
    #         twins[t.canon.id].add(t.id)

    instruction_text = Text.from_markup(
        "[bold]Enter[/bold] on empty selection - to mark all as canons\n"
        "[bold]1[/bold] - to mark 1 as a canon and others as twins\n"
        "[bold]1: 2 3[/bold] - to mark 1 as a canon and 2 3 as twins\n",
    )
    skip = False

    selection_text = Text()
    while True:
        table = compose_table(key, tracks)

        if twins:
            for canon, canon_twins in twins.items():
                if selection_text:
                    selection_text.append("\n")
                selection_text.append(f"• Canon {canon}: ", style="bold green")
                selected_twins = ", ".join(map(str, sorted(canon_twins)))
                selection_text.append(f"Twins [{selected_twins}]", style="yellow")
        else:
            if selection_text:
                selection_text.append("\n")
            selection_text.append("Nothing selected yet", style="dim white")

        ui_group = Group(
            table,
            Panel(selection_text, title="Selection", border_style="blue"),
            Panel(
                instruction_text,
                title="Instructions",
                subtitle="[[bold]s[/bold]] skip | [[bold]Enter[/bold]] finish",
                style="dim",
            ),
            progress,
        )

        console.clear()
        console.print(ui_group)

        entry = Prompt.ask("Write here")
        if entry.strip().lower() == "s":
            twins = {}  # reset the choices
            skip = True
            selection_text = Text("The group will be skipped. Enter to continue...", style="bold yellow")
            continue

        if not entry:
            if skip:
                break
            if not twins:
                # no choices are made, means the group is all canons
                for id_ in ids:
                    twins[id_] = set()
                    selection_text = Text()
                continue
            break

        if entry.strip().isdigit():
            # entry = canon
            # the rest are twins
            canon_id = int(entry.strip())
            # the rest of available indices that are neither the canon nor the twin
            twin_ids = {
                id_ for id_ in ids if id_ != canon_id and id_ not in occupied_twin_ids and id_ not in twins
            }
        else:
            try:
                # entry = canon: twin1, twin2, twin3
                canon_part, twin_part = entry.split(":")
                canon_id = int(canon_part.strip())
                twin_ids = {int(x.strip()) for x in twin_part.split()}
            except ValueError:
                selection_text = Text("Can't parse:", style="bold red")
                selection_text.append(" use format 'canon_id[: twin_id[ twin_id]]'")
                continue

        try:
            # second round of validation
            if canon_id not in ids:
                raise ValueError(f"ID not found: {canon_id}")
            if twin_ids.difference(ids):
                raise ValueError(f"IDs not found: {twin_ids.difference(ids)}")
            if canon_id in occupied_twin_ids:
                raise ValueError(f"Can't use the twin as a canon: {canon_id}")
            if twin_ids.intersection(set(twins)):
                raise ValueError(f"Can't use the canon as a twin: {twin_ids.intersection(set(twins))}")
            if twin_ids.intersection(occupied_twin_ids):
                raise ValueError(f"Can't use the same twin twice: {twin_ids.intersection(occupied_twin_ids)}")
            if not twin_ids:
                raise ValueError("No twin IDs specified")
        except ValueError as e:
            selection_text = Text(f"{e}", style="bold red")
            continue

        occupied_twin_ids.update(twin_ids)
        twins[canon_id].update(twin_ids)
        selection_text = Text()

    groups = []
    for canon_id, twin_ids in twins.items():
        dg = DupGroup(canon_id=canon_id, twin_ids=sorted(twin_ids))
        groups.append(dg)
    return groups


def deduplicate_group(key: str, tracks: list[Track], s: Session):
    groups: list[DupGroup] = []
    if key in DUPES:
        # cached choices
        groups: list[DupGroup] = DUPES[key]
        # if groups:
        # print("cached:", key, groups)

    if not groups:
        groups = prompt_duplicate_group(key, tracks)

    for dg in groups:
        for twin_id in dg.twin_ids:
            twin = [t for t in tracks if t.id == twin_id][0]
            if twin.canon_id and twin.canon_id != dg.canon_id:
                raise ValueError(f"Twin {twin.id} already has a different canon: {twin.canon_id}")
            twin.canon_id = dg.canon_id
    s.flush()

    DUPES[key] = groups


def deduplicate_tracks(s: Session):
    for cols in (
        (Track.artist_norm, Track.title_norm),
        (Track.album_artist_norm, Track.title_norm),
        (Track.album_norm, Track.title_norm),
    ):
        combinations = s.exec(
            select(*cols, func.count(Track.id).label("count"))
            .where(Track.canon_id.is_(None))  # exclude tracks already marked as twins
            .group_by(*cols)
            .having(func.count(Track.id) > 1)
            .order_by(*cols)
        ).all()

        col_names = "/".join([c.name for c in cols])
        task_id = progress.add_task(f"Duplicates by {col_names}", total=len(combinations))
        try:
            for *col_vals, count in combinations:
                col_to_val = list(zip(cols, col_vals))
                tracks = s.exec(
                    select(Track)
                    .where(Track.canon_id.is_(None), *[col == val for col, val in col_to_val])
                    .order_by(Track.id)
                ).all()

                key = ", ".join([f"{col.name}: {val}" for col, val in col_to_val])
                deduplicate_group(key, list(tracks), s)
                progress.update(task_id, advance=1)

            # persist all changes
            s.commit()
            # also save choices to a filesystem
            DupGroup.dump(DUPES, settings.duplicates_filepath)
            print("Finished deduplication by", col_names)

        except KeyboardInterrupt:
            # save choices permanently on ctrl+c, dont commit
            DupGroup.dump(DUPES, settings.duplicates_filepath)
            print()
            print("saved choices to the filesystem")
            raise
