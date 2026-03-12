from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from sqlmodel import Session, func, select

from airdrome.conf import settings
from airdrome.models import Track

from .schemas import DupGroup


DUPES: dict[str, DupGroup] = DupGroup.load(settings.duplicates_filepath)

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


def compose_table(key: str, tracks: list[Track], canons: list[int | None]):
    table = Table(title=f"Duplicates by {key}")
    table.add_column("Index", style="blue")
    table.add_column("Canon ID", style="blue")
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
            table.add_row(f"{i + 1}", f"{canons[i] or '-'}", *row, **row_kw)

    return table


def prompt_duplicate_group(key: str, tracks: list[Track]) -> DupGroup | None:
    members = [t.id for t in tracks]
    canons = [t.canon_id for t in tracks]

    instruction_text = Text.from_markup(
        "[bold]Enter[/bold] to confirm current state\n"
        "[bold]1[/bold] - to mark 1 as a canon and others as twins\n"
        "[bold]1 2 3[/bold] - to mark 1 as a canon of 2 and 3\n",
    )
    skip = confirm = False
    feedback_text = Text()
    while True:
        table = compose_table(key, tracks, canons)

        ui_group = Group(
            table,
            Panel(feedback_text, title="Feedback", border_style="dim blue"),
            Panel(
                instruction_text,
                title="Instructions",
                subtitle="[[bold]r[/bold]] reset choices | "
                "[[bold]s[/bold]] skip group | "
                "[[bold]Enter[/bold]] finish",
                style="dim",
            ),
            progress,
        )

        console.clear()
        console.print(ui_group)

        entry = Prompt.ask("Write here")
        if not entry:
            # Enter to confirm choices
            if not confirm:
                confirm = True
                feedback_text = Text("Enter to confirm", style="bold yellow")
                continue
            break

        if entry.strip().lower() == "s":
            skip = confirm = True
            feedback_text = Text("The group will be skipped. Enter to continue...", style="bold yellow")
            continue

        elif entry.strip().lower() == "r":
            # reset choices
            canons = [t.canon_id for t in tracks]
            feedback_text = Text("Choices are reset", style="bold yellow")
            continue

        if entry.strip().isdigit():
            # entry = "canon_idx"
            # the rest are twins
            canon_idx = int(entry.strip()) - 1
            member_idxs = [i for i in range(len(members)) if i != canon_idx]
        else:
            # this is not a digit, means this is 2 digits
            try:
                # entry = "canon_idx twin_idx[ twin_idx]"
                canon_idx, *member_idxs = [int(v) - 1 for v in entry.split()]
            except ValueError:
                feedback_text = Text("Can't parse:", style="bold red")
                feedback_text.append(" use format ")
                feedback_text.append("canon_idx[ twin_idx]", style="bold")
                continue

        try:
            # second round of validation
            for idx in (canon_idx, *member_idxs):
                if idx not in range(len(members)):
                    raise ValueError(f"Index out of range: {canon_idx + 1}")

            for member_idx in member_idxs:
                if canon_idx == member_idx:
                    raise ValueError(f"Can't make the track as canon of itself: {canon_idx + 1}")
                if members[member_idx] in canons:
                    raise ValueError(f"Already chosen as a canon: {member_idx + 1}")
                # already a twin
                if canons[canon_idx]:
                    raise ValueError(f"Already has a canon: {canon_idx + 1}")
        except ValueError as e:
            feedback_text = Text(f"{e}", style="bold red")
            continue

        for member_idx in member_idxs:
            canons[member_idx] = members[canon_idx]

        confirm = True
        feedback_text = Text()

    if skip:
        return None

    return DupGroup(members=members, canons=canons)


def deduplicate_group(key: str, tracks: list[Track], s: Session):
    group: DupGroup | None = None
    if key in DUPES:
        # cached choices
        group: DupGroup = DUPES[key]

    if not group:
        group = prompt_duplicate_group(key, tracks)

    if not group:
        # skip this group
        return

    for i in range(len(group.members)):
        if group.canons[i]:
            # this track has a canon, means it is a twin
            track = tracks[i]
            if track.canon_id and track.canon_id != group.canons[i]:
                raise ValueError(
                    f"Track {track.id} already has a canon: current {track.canon_id}, new {group.canons[i]}"
                )
            track.canon_id = group.canons[i]
    s.flush()

    DUPES[key] = group


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
