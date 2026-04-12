from dataclasses import dataclass, field

from rich.console import Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from sqlmodel import Column, Session, func, select

from airdrome.console import console
from airdrome.models import Track


INSTRUCTION_TEXT = Text.from_markup(
    "[bold]1[/bold] - mark 1 as canon, others as twins\n"
    "[bold]1 2 3[/bold] - mark 1 as canon of 2 and 3\n"
    "[bold]r[/bold] - reset choices for this group\n"
    "[bold]a[/bold] / [bold]d[/bold] - previous / next group\n"
    "[bold]c[/bold] - commit changes\n"
    "[bold]q[/bold] - exit\n"
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


@dataclass
class Page:
    tracks: list[Track]
    canons: list[int | None]
    chosen_canons: list[int | None] = field(default_factory=list)
    confirmed: bool = False  # whether the user confirmed the choices


@dataclass
class DeduplicatorState:
    pages: dict[str, Page] = field(default_factory=dict)
    current_idx: int = field(default=0)
    pages_iter: list[tuple[str, Page]] = field(default_factory=list)

    def __post_init__(self):
        self.pages_iter = list(self.pages.items())


class Deduplicator:
    COLUMN_SETS = [
        [Track.artist_norm, Track.title_norm],
        [Track.album_artist_norm, Track.title_norm],
        [Track.album_norm, Track.title_norm],
    ]

    def __init__(self, s: Session):
        self.s = s
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
        )
        self.state: DeduplicatorState = DeduplicatorState()  # empty, filled by fill_state()
        self.feedback_text = Text()

    def render_page(self, key: str, page: Page):
        table = compose_table(key, page.tracks, page.chosen_canons)
        ui_group = Group(
            table,
            Panel(self.feedback_text, title="Feedback", border_style="dim blue"),
            Panel(INSTRUCTION_TEXT, title="Instructions", style="dim"),
            self.progress,
        )
        console.clear()
        console.print(ui_group)

    def handle_input(self, entry: str, page: Page) -> str | None:
        """Process one input entry against the given page.

        Mutates page.chosen_canons in place.
        Returns (feedback_text, action) where action is "next", "prev", "commit", or None (stay).
        """
        self.feedback_text = Text()
        cmd = entry.strip().lower()
        if cmd == "":
            # confirm the group setup, even if no changes were made
            return "confirm"
        if cmd == "q":
            return "exit"
        if cmd == "a":
            return "prev"
        if cmd == "d":
            return "next"
        if cmd == "c":
            return "commit"
        if cmd == "r":
            page.chosen_canons = list(page.canons)
            self.feedback_text = Text("Choices reset", style="bold yellow")
            return None

        members = [t.id for t in page.tracks]
        chosen = page.chosen_canons

        if entry.strip().isdigit():
            canon_idx = int(entry.strip()) - 1
            member_idxs = [i for i in range(len(members)) if i != canon_idx]
        else:
            try:
                canon_idx, *member_idxs = [int(v) - 1 for v in entry.split()]
            except ValueError:
                feedback = Text("Can't parse:", style="bold red")
                feedback.append(" use format ")
                feedback.append("canon_idx[ twin_idx]", style="bold")
                self.feedback_text = feedback
                return None

        try:
            for idx in (canon_idx, *member_idxs):
                if idx not in range(len(members)):
                    raise ValueError(f"Index out of range: {idx + 1}")
            for member_idx in member_idxs:
                if canon_idx == member_idx:
                    raise ValueError(f"Can't mark track as canon of itself: {canon_idx + 1}")
                if members[member_idx] in chosen:
                    raise ValueError(f"Already chosen as a canon: {member_idx + 1}")
                if chosen[canon_idx]:
                    raise ValueError(f"Already has a canon: {canon_idx + 1}")
        except ValueError as e:
            self.feedback_text = Text(f"{e}", style="bold red")
            return None

        for member_idx in member_idxs:
            page.chosen_canons[member_idx] = members[canon_idx]

        return None

    def apply_changes(self) -> int:
        changed = 0
        for key, page in self.state.pages_iter:
            if not page.confirmed:
                # do not commit unconfirmed changes
                continue
            for i, track in enumerate(page.tracks):
                new_canon = page.chosen_canons[i]
                if new_canon != page.canons[i]:
                    track.canon_id = new_canon
                    self.s.add(track)
                    changed += 1
        if changed:
            self.s.commit()
        return changed

    def _update(self, task_id):
        self.progress.update(task_id, completed=self.state.current_idx + 1)

    def _serve(self):
        if not self.state.pages_iter:
            console.print("[green]No duplicates found.[/green]")
            return

        total = len(self.state.pages_iter)
        task_id = self.progress.add_task("Deduplicating", total=total)
        self._update(task_id)

        self.feedback_text = Text()
        while True:
            key, page = self.state.pages_iter[self.state.current_idx]
            self.render_page(key, page)
            entry = Prompt.ask("Write here")
            action = self.handle_input(entry, page)

            if action == "next":
                if self.state.current_idx < total - 1:
                    self.state.current_idx += 1
                    self._update(task_id)
                    self.feedback_text = Text()
            elif action == "prev":
                if self.state.current_idx > 0:
                    self.state.current_idx -= 1
                    self._update(task_id)
                    self.feedback_text = Text()
            elif action == "confirm":
                page.confirmed = True
                self.state.current_idx += 1
                self._update(task_id)
                self.feedback_text = Text()
            elif action == "commit":
                n = self.apply_changes()
                self.feedback_text = Text(f"{n} change(s) committed.", style="bold green")
            elif action == "exit":
                console.print("[green]Exited.[/green]")
                return

    def get_track_groups(self, cols: list[Column]) -> list[tuple[str, list[Track]]]:
        combinations = self.s.exec(
            select(*cols, func.count(Track.id).label("count"))
            # .where(Track.canon_id.is_(None))  # exclude tracks already marked as twins
            .group_by(*cols)
            .having(func.count(Track.id) > 1)
            .order_by(*cols)
        )
        groups = []
        for *col_vals, count in combinations:
            col_to_val = list(zip(cols, col_vals))
            key = ",".join(f"{c.name}={v}" for c, v in col_to_val)
            track_group = list(
                self.s.exec(
                    select(Track)
                    .where(
                        # Track.canon_id.is_(None),
                        *[col == val for col, val in col_to_val]
                    )
                    .order_by(Track.id)
                )
            )
            groups.append((key, track_group))
        return groups

    def fill_state(self):
        pages = {}
        for cols in self.COLUMN_SETS:
            for key, tracks in self.get_track_groups(cols):
                pages[key] = Page(
                    tracks=tracks,
                    canons=[t.canon_id for t in tracks],
                    chosen_canons=[t.canon_id for t in tracks],
                )
        self.state = DeduplicatorState(pages=pages, current_idx=0)

    def run(self):
        # fill the buffer with all the duplicate track groups
        self.fill_state()
        # run the selection UI
        self._serve()
