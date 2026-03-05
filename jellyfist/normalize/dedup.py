from collections import defaultdict

from sqlmodel import Session, select, func

from jellyfist.conf import settings
from jellyfist.models import Track
from .schemas import DupGroup
from rich.table import Table
from rich.console import Console
from rich.prompt import Prompt

DUPES: dict[str, list[DupGroup]] = DupGroup.load(settings.duplicates_filepath)

console = Console()


def get_table_rows(t: Track) -> list[list[str]]:
    rows = []
    base_row = [
        str(t.track_n) if t.track_n is not None else "",
        str(t.disc_n) if t.disc_n is not None else "",
        str(len(t.files)) if len(t.files) else "",
        t.title,
        t.artist or "",
        t.album_artist or "",
        t.album or "",
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


def render_duplicate_group(key: str, tracks: list[Track]):
    table = Table(title=f"Duplicates by {key}")
    table.add_column("Index", style="blue")
    table.add_column("Track №", style="pink3")
    table.add_column("Disc №", style="pink3")
    table.add_column("Files", style="yellow")
    table.add_column("Title", style="orange4")
    table.add_column("Artist", style="magenta")
    table.add_column("Album Artist", style="green")
    table.add_column("Album", style="yellow")

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
        for row in rows:
            table.add_row(str(i + 1), *row, **row_kw)

    console.print(table)


def prompt_duplicate_group(key: str, tracks: list[Track]) -> list[DupGroup]:
    render_duplicate_group(key, tracks)
    console.print('"1"      to mark 1 as a canon and others as twins')
    console.print('"1: 2 3" to mark 1 as a canon and 2 3 as twins')

    # canon_idx: set[twin_idx]
    twins = defaultdict(set)

    # populate keys with current canons
    for i, t in enumerate(tracks):
        if t.twins:
            twins[i + 1] = set()

    # available indices
    idxs = {v + 1 for v in range(len(tracks))}
    # indices selected as twins (to check against)
    occupied_twin_idxs = set()

    while True:
        if twins:
            for canon, canon_twins in twins.items():
                console.print(f"{canon}: {', '.join([str(v) for v in sorted(canon_twins)])}")

        entry = Prompt.ask("Indices, [bold]Enter[/bold] to finish, [bold]s[/bold] to skip")
        if entry.strip().lower() == "s":
            twins = {}  # reset the choices
            console.print("[yellow]Group skipped[/yellow]")
            break

        if not entry:
            if not twins:
                # no choices are made, means the group has no duplicates
                for idx in idxs:
                    twins[idx] = set()
                console.print("[yellow]Canon only group[/yellow]")
            break

        if entry.strip().isdigit():
            # entry = canon
            # the rest are twins
            canon_idx = int(entry.strip())
            # the rest of available indices that are neither the canon nor the twin
            twin_idxs = {v for v in idxs if v != canon_idx and v not in occupied_twin_idxs and v not in twins}
        else:
            try:
                # entry = canon: twin1, twin2, twin3
                canon_part, twin_part = entry.split(":")
                canon_idx = int(canon_part.strip())
                twin_idxs = {int(x.strip()) for x in twin_part.split()}
            except ValueError:
                console.print("[red]Can't parse:[/red] use format 'canon_index[: twin_index[ twin_index]]'")
                continue

        try:
            # second round of validation
            if canon_idx not in idxs:
                raise ValueError(f"Canon index out of range: {canon_idx}")
            if twin_idxs.difference(idxs):
                raise ValueError(f"Twin indices out of range: {twin_idxs.difference(idxs)}")
            if canon_idx in occupied_twin_idxs:
                raise ValueError(f"Can't use the twin as a canon: {canon_idx}")
            if twin_idxs.intersection(set(twins)):
                raise ValueError(f"Can't use the canon as a twin: {twin_idxs.intersection(set(twins))}")
            if twin_idxs.intersection(occupied_twin_idxs):
                raise ValueError(
                    f"Can't use the same twin twice: {twin_idxs.intersection(occupied_twin_idxs)}"
                )
            if not twin_idxs:
                raise ValueError("No twin indices specified")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            continue

        occupied_twin_idxs.update(twin_idxs)
        twins[canon_idx].update(twin_idxs)

    groups = []
    for canon_idx, twin_idxs in twins.items():
        dg = DupGroup(canon_id=tracks[canon_idx - 1].id, twin_ids=[tracks[idx - 1].id for idx in twin_idxs])
        groups.append(dg)
    return groups


def deduplicate_group(key: str, tracks: list[Track], s: Session):
    groups: list[DupGroup] = []
    if key in DUPES:
        # cached choices
        groups: list[DupGroup] = DUPES[key]
        if groups:
            print("cached:", key, groups)

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
        total_groups = len(combinations)
        print(f"Found {total_groups} groups by", "/".join([c.name for c in cols]))

        try:
            for i, (*col_vals, count) in enumerate(combinations):
                col_to_val = list(zip(cols, col_vals))
                tracks = s.exec(
                    select(Track)
                    .where(Track.canon_id.is_(None), *[col == val for col, val in col_to_val])
                    .order_by(Track.id)
                ).all()

                key = ", ".join([f"{col.name}: {val}" for col, val in col_to_val])
                print(f"{i + 1:>3}/{total_groups:>3}")
                deduplicate_group(key, list(tracks), s)

            # persist all changes
            s.commit()
            # also save choices to a filesystem
            DupGroup.dump(DUPES, settings.duplicates_filepath)

        except KeyboardInterrupt:
            # save choices permanently on ctrl+c, dont commit
            DupGroup.dump(DUPES, settings.duplicates_filepath)
            print()
            print("saved choices to the filesystem")
            raise
