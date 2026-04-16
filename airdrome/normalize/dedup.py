import json
from dataclasses import dataclass, field
from pathlib import Path

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


def get_table_row(t: Track) -> dict[str, str]:
    row = {
        "ID": str(t.id),
        "Title": t.title,
        "Artist": t.artist or "",
        "Album artist": t.album_artist or "",
        "Album": t.album or "",
        "Track #": str(t.track_n) if t.track_n is not None else "",
        "Disc #": str(t.disc_n) if t.disc_n is not None else "",
        "Compilation": "yes" if t.compilation else "",
        "Year": str(t.year) or "",
        "Duration": f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "",
        "Date added": t.date_added.strftime("%Y-%m-%d %H:%M:%S"),
        "Loved": "yes" if t.loved else "",
        "Album loved": "yes" if t.album_loved else "",
        "Files": str(len(t.files)) if len(t.files) else "",
        "XML": str(len(t.apple_tracks)) if t.apple_tracks else "",
        "AMS": str(len(t.apple_ms_tracks)) if t.apple_ms_tracks else "",
    }
    return row


def compose_table(key: str, tracks: list[Track], canons: list[int | None]):
    table = Table(title=f"Duplicates by {key}")
    table.add_column("Index", style="blue")
    table.add_column("Canon ID", style="blue")

    for h in get_table_row(tracks[0]).keys():
        style = "yellow"
        if h in ("Date added", "Loved", "Album loved", "Files"):
            style = "green"
        if h in ("XML", "AMS"):
            style = "red"
        table.add_column(h, style=style)

    for i, t in enumerate(tracks):
        row_kw = {}
        if t.twins:
            row_kw["style"] = "bold"
        if t.canon_id:
            row_kw["style"] = "dim"
        row = get_table_row(t).values()
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

    def __init__(self, s: Session, filepath: Path):
        self.s = s
        self.filepath = filepath
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
            Panel(
                self.feedback_text
                or (
                    Text("confirmed", style="dim blue")
                    if page.confirmed
                    else Text("Unconfirmed", style="dim yellow")
                ),
                title="Feedback",
                border_style="dim blue",
            ),
            Panel(INSTRUCTION_TEXT, title="Instructions", style="dim"),
            self.progress,
        )
        console.clear()
        console.print(ui_group)

    def handle_input(self, entry: str, page: Page) -> str | None:
        """Process one input entry against the given page.

        Mutates page.chosen_canons in place.
        Returns (feedback_text, action) where the action is "next", "prev", "commit", or None (stay).
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
            return "reset"

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
        page.confirmed = False

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
            self._dump(self.filepath)
        return changed

    def _dump(self, path: Path) -> None:
        data: dict = {}
        for key, page in self.state.pages_iter:
            if not page.confirmed:
                continue
            id_to_hash = {t.id: t.duplicate_hash for t in page.tracks}
            data[key] = {
                "members": [t.duplicate_hash for t in page.tracks],
                "canon_hashes": [
                    id_to_hash.get(canon_id) if canon_id is not None else None
                    for canon_id in page.chosen_canons
                ],
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            data: dict = json.load(f)
        for key, saved in data.items():
            page = self.state.pages.get(key)
            if page is None:
                continue
            hash_to_id = {t.duplicate_hash: t.id for t in page.tracks}
            current_hashes = [t.duplicate_hash for t in page.tracks]
            if current_hashes != saved.get("members", []):
                continue
            canon_hashes: list = saved.get("canon_hashes", [])
            if len(canon_hashes) != len(page.tracks):
                continue
            restored: list[int | None] = []
            for canon_hash in canon_hashes:
                if canon_hash is None:
                    restored.append(None)
                else:
                    resolved = hash_to_id.get(canon_hash)
                    if resolved is None:
                        break
                    restored.append(resolved)
            else:
                page.chosen_canons = restored
                page.confirmed = True

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
            if self.state.current_idx >= total:
                console.print("[green]All groups reviewed.[/green]")
                if self.filepath is not None:
                    self._dump(self.filepath)
                return

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
                if page.confirmed:
                    self.state.current_idx += 1
                    self._update(task_id)
                    self.feedback_text = Text()
                else:
                    page.confirmed = True
            elif action == "reset":
                page.confirmed = False
                page.chosen_canons = list(page.canons)
                self.feedback_text = Text()

            elif action == "commit":
                n = self.apply_changes()
                self.feedback_text = Text(f"{n} change(s) committed.", style="bold green")
            elif action == "exit":
                if self.filepath is not None:
                    self._dump(self.filepath)
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
        if self.filepath is not None:
            self._load(self.filepath)

    def run(self):
        # fill the buffer with all the duplicate track groups
        self.fill_state()
        # run the selection UI
        self._serve()


def auto_deduplicate(
    session: Session,
    with_artist: bool = True,
    with_album_artist: bool = True,
    with_album: bool = True,
    with_year: bool = True,
    with_track_n: bool = True,
    with_disc_n: bool = True,
    with_duration: bool = True,
    dry_run: bool = False,
) -> list[list[Track]]:
    """Auto-mark twins for tracks with identical normalized metadata.

    Only processes tracks where canon_id IS NULL (unreviewed). For each
    matching group, the track with the lowest ID becomes the canonical one;
    the rest are marked as twins. No metadata is written back to the canonical
    track — aggregated values are derived from the group at use time.

    Title and artist are always required. Album artist, album, track number,
    disc number, and duration can each be excluded to loosen matching.

    Returns the resolved groups (each group[0] is the canon). Pass dry_run=True
    to compute the groups without writing to the database.
    """
    tracks = list(session.exec(select(Track).where(Track.canon_id.is_(None)).order_by(Track.id)))

    def group_key(t: Track) -> tuple:
        key: list = [t.title_norm]
        if with_artist:
            key.append(t.artist_norm)
        if with_album_artist:
            key.append(t.album_artist_norm)
        if with_album:
            key.append(t.album_norm)
        if with_track_n:
            key.append(t.track_n)
        if with_disc_n:
            key.append(t.disc_n)
        if with_duration:
            key.append(round(t.duration / 5) * 5 if t.duration is not None else None)
        if with_year:
            key.append(t.year)
        return tuple(key)

    bucketed: dict[tuple, list[Track]] = {}
    for t in tracks:
        bucketed.setdefault(group_key(t), []).append(t)

    resolved: list[list[Track]] = []
    for group_tracks in bucketed.values():
        if len(group_tracks) < 2:
            continue
        resolved.append(group_tracks)  # already sorted by ID (ascending) from the query
        if not dry_run:
            canon = group_tracks[0]
            for twin in group_tracks[1:]:
                twin.canon_id = canon.id
                session.add(twin)

    if not dry_run and resolved:
        session.commit()

    return resolved
