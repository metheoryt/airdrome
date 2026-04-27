import json
from dataclasses import dataclass, field
from enum import Enum
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
    "[bold]Enter[/bold] - confirm current group\n"
    "[bold]r[/bold] - reset choices for this group\n"
    "[bold]a[/bold] / [bold]d[/bold] - previous / next group\n"
    "[bold]m[/bold] - cycle mode: resolved / auto-resolved\n"
    "[bold]c[/bold] - commit changes\n"
    "[bold]q[/bold] - exit\n"
)


class FilterMode(Enum):
    RESOLVED = "resolved"
    AUTO_RESOLVED = "auto-resolved"

    def next(self) -> "FilterMode":
        members = list(FilterMode)
        return members[(members.index(self) + 1) % len(members)]


@dataclass
class Page:
    tracks: list[Track]
    canons: list[int | None] = field(default_factory=list)
    chosen_canons: list[int | None] = field(default_factory=list)
    confirmed: bool = False
    auto_resolved: bool = False  # canon_ids already set in DB by auto-dedup

    def __post_init__(self):
        self.canons = [t.canon_id for t in self.tracks]
        self.chosen_canons = list(self.canons)
        self.auto_resolved = any(c is not None for c in self.canons)

    def confirm(self) -> None:
        self.confirmed = True
        self.auto_resolved = False

    def reset(self) -> None:
        self.confirmed = False
        self.auto_resolved = False
        self.chosen_canons = [None] * len(self.tracks)

    def set_canon(self, canon_idx: int, member_idxs: list[int]) -> None:
        members = [t.id for t in self.tracks]
        for idx in (canon_idx, *member_idxs):
            if idx not in range(len(members)):
                raise ValueError(f"Index out of range: {idx + 1}")
        for member_idx in member_idxs:
            if canon_idx == member_idx:
                raise ValueError(f"Can't mark track as canon of itself: {canon_idx + 1}")
            if members[member_idx] in self.chosen_canons:
                raise ValueError(f"Already chosen as a canon: {member_idx + 1}")
            if self.chosen_canons[canon_idx]:
                raise ValueError(f"Already has a canon: {canon_idx + 1}")
        for member_idx in member_idxs:
            self.chosen_canons[member_idx] = members[canon_idx]
        self.confirmed = False


@dataclass
class DeduplicatorState:
    pages: dict[str, Page] = field(default_factory=dict)
    current_idx: int = 0
    filter_mode: FilterMode = FilterMode.RESOLVED
    partial_match: str = ""
    pages_iter: list[tuple[str, Page]] = field(default_factory=list, init=False)
    _mode_idx: dict = field(default_factory=lambda: {m: 0 for m in FilterMode}, init=False)

    def __post_init__(self):
        self.pages_iter = list(self.pages.items())

    def filtered_pages(self) -> list[tuple[str, Page]]:
        pages = self.pages_iter
        if self.partial_match:
            pages = [
                (k, p)
                for k, p in pages
                if any(
                    self.partial_match in v
                    for t in p.tracks
                    for v in (t.title_norm, t.artist_norm, t.album_artist_norm, t.album_norm)
                    if v is not None
                )
            ]
        match self.filter_mode:
            case FilterMode.RESOLVED:
                return [(k, p) for k, p in pages if not p.auto_resolved]
            case FilterMode.AUTO_RESOLVED:
                return [(k, p) for k, p in pages if p.auto_resolved and not p.confirmed]

    def switch_mode(self) -> None:
        self._mode_idx[self.filter_mode] = self.current_idx
        self.filter_mode = self.filter_mode.next()
        new_total = len(self.filtered_pages())
        saved = self._mode_idx[self.filter_mode]
        self.current_idx = min(saved, new_total - 1) if new_total > 0 else 0

    def current_page(self) -> tuple[str, Page] | None:
        filtered = self.filtered_pages()
        if not filtered:
            return None
        return filtered[self.current_idx]

    def clamp(self) -> None:
        total = len(self.filtered_pages())
        if total > 0 and self.current_idx >= total:
            self.current_idx = total - 1

    def go_next(self) -> bool:
        total = len(self.filtered_pages())
        if self.current_idx < total - 1:
            self.current_idx += 1
            return True
        return False

    def go_prev(self) -> bool:
        if self.current_idx > 0:
            self.current_idx -= 1
            return True
        return False

    def dump(self, path: Path) -> None:
        data: dict = {}
        for key, page in self.pages_iter:
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

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            data: dict = json.load(f)
        for key, saved in data.items():
            page = self.pages.get(key)
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
                page.auto_resolved = False  # human-confirmed overrides auto-resolved status


class DeduplicatorUI:
    def __init__(self, deduplicator: "Deduplicator"):
        self.dedup = deduplicator
        self.state = deduplicator.state
        self.feedback_text = Text()
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
        )

    @staticmethod
    def _get_table_row(t: Track) -> dict[str, str]:
        return {
            "ID": str(t.id),
            "Title": t.title,
            "Artist": t.artist or "",
            "Album artist": t.album_artist or "",
            "Album": t.album or "",
            "Track #": str(t.track_n) if t.track_n is not None else "",
            "Disc #": str(t.disc_n) if t.disc_n is not None else "",
            "Comp": "✅" if t.compilation else "",
            "Year": str(t.year) or "",
            "Time": f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "",
            "Files": "\n".join([f.duration_str or "--:--" for f in t.files]) if len(t.files) else "",
            "Date added": t.date_added.strftime("%Y-%m-%d"),
            "❤️": "yes" if t.loved else "",
            "Album ❤️": "yes" if t.album_loved else "",
            "XML": str(len(t.apple_tracks)) if t.apple_tracks else "",
            "AMS": str(len(t.apple_ms_tracks)) if t.apple_ms_tracks else "",
        }

    @staticmethod
    def compose_table(key: str, tracks: list[Track], canons: list[int | None]) -> Table:
        table = Table(title=f"Duplicates by {key}")
        table.add_column("Index", style="blue")
        table.add_column("Canon ID", style="blue")

        for h in DeduplicatorUI._get_table_row(tracks[0]).keys():
            style = "yellow"
            if h in ("Date added", "❤️", "Album ❤️", "Files"):
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
            row = DeduplicatorUI._get_table_row(t).values()
            table.add_row(f"{i + 1}", f"{canons[i] or '-'}", *row, **row_kw)

        return table

    def render_page(self, key: str, page: Page, filtered_total: int) -> None:
        table = self.compose_table(key, page.tracks, page.chosen_canons)

        if page.confirmed:
            status_text = Text("confirmed", style="dim blue")
        elif page.auto_resolved:
            status_text = Text("auto-resolved", style="dim yellow")
        else:
            status_text = Text("unconfirmed", style="dim red")

        header = Text(f"[{self.state.filter_mode.value}] {self.state.current_idx + 1}/{filtered_total}  ")
        header.append_text(status_text)
        if self.state.partial_match:
            header.append_text(Text(f" / partial match: {self.state.partial_match}"))

        feedback_content = Group(header, self.feedback_text) if self.feedback_text else header

        ui_group = Group(
            table,
            Panel(feedback_content, title="Feedback", border_style="dim blue"),
            Panel(INSTRUCTION_TEXT, title="Instructions", style="dim"),
            self.progress,
        )
        console.clear()
        console.print(ui_group)

    def handle_input(self, entry: str, page: Page) -> str | None:
        """Process one input entry. Returns an action string or None."""
        self.feedback_text = Text()
        cmd = entry.strip().lower()
        if cmd == "":
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
        if cmd == "m":
            return "mode"

        if entry.strip().isdigit():
            canon_idx = int(entry.strip()) - 1
            member_idxs = [i for i in range(len(page.tracks)) if i != canon_idx]
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
            page.set_canon(canon_idx, member_idxs)
        except ValueError as e:
            self.feedback_text = Text(f"{e}", style="bold red")

        return None

    def _update(self, task_id, total: int) -> None:
        self.progress.update(
            task_id,
            description=f"[bold blue]{self.state.filter_mode.value}",
            total=max(total, 1),
            completed=min(self.state.current_idx + 1, total),
        )

    def serve(self) -> None:
        if not self.state.pages_iter:
            console.print("[green]No duplicates found.[/green]")
            return

        task_id = self.progress.add_task(self.state.filter_mode.value, total=1)
        self.feedback_text = Text()

        while True:
            filtered = self.state.filtered_pages()
            total = len(filtered)

            self.state.clamp()
            self._update(task_id, total)

            if total == 0:
                console.clear()
                empty_msg = Text(f"No groups in [{self.state.filter_mode.value}] mode.", style="dim")
                console.print(Panel(empty_msg, title="Feedback", border_style="dim blue"))
                console.print(self.progress)
                entry = Prompt.ask("Write here")
                cmd = entry.strip().lower()
                if cmd == "q":
                    self.state.dump(self.dedup.filepath)
                    console.print("[green]Exited.[/green]")
                    return
                elif cmd == "m":
                    self.state.switch_mode()
                elif cmd == "c":
                    n = self.dedup.apply_changes()
                    self.feedback_text = Text(f"{n} change(s) committed.", style="bold green")
                continue

            key, page = filtered[self.state.current_idx]
            self.render_page(key, page, total)
            entry = Prompt.ask("Write here")
            action = self.handle_input(entry, page)

            if action == "next":
                self.state.go_next()
                self.feedback_text = Text()
            elif action == "prev":
                self.state.go_prev()
                self.feedback_text = Text()
            elif action == "confirm":
                if page.confirmed:
                    self.state.go_next()
                    self.feedback_text = Text()
                else:
                    page.confirm()
            elif action == "reset":
                page.reset()
                self.feedback_text = Text()
            elif action == "commit":
                n = self.dedup.apply_changes()
                self.feedback_text = Text(f"{n} change(s) committed.", style="bold green")
            elif action == "mode":
                self.state.switch_mode()
                self.feedback_text = Text()
            elif action == "exit":
                self.state.dump(self.dedup.filepath)
                console.print("[green]Exited.[/green]")
                return


class Deduplicator:
    COLUMN_SETS = [
        [Track.artist_norm, Track.title_norm],
        [Track.album_artist_norm, Track.title_norm],
        [Track.album_norm, Track.title_norm],
    ]

    def __init__(self, s: Session, filepath: Path, partial_match: str = ""):
        self.s = s
        self.filepath = filepath
        self.state = DeduplicatorState(partial_match=partial_match)

    def get_track_groups(self, cols: list[Column]) -> list[tuple[str, list[Track]]]:
        combinations = self.s.exec(
            select(*cols, func.count(Track.id).label("count"))
            # do not exclude them, since they can appear in a broader group
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

    def dedup_pages(self, groups: list[tuple[str, list[Track]]]) -> list[tuple[str, list[Track]]]:
        """
        Deduplicate track pages:
            remove duplicate groups or smaller subgroups,
            produced by different column sets.
        Leave the biggest group.
        """
        id_tups = [tuple(sorted([t.id for t in tracks if t.id is not None])) for _, tracks in groups]
        id_tups.sort(key=len, reverse=True)
        seen = set()
        for id_tup in id_tups:
            # starting from the longest sets
            # 1. same group check
            if id_tup in seen:
                continue
            id_set = set(id_tup)
            # 2. subgroup check
            if any([id_set.issubset(set(v)) for v in seen]):
                continue

            seen.add(id_tup)

        # construct groups back from seen, keep original order
        new_groups = []
        for key, tracks in groups:
            id_tup = tuple(sorted([t.id for t in tracks if t.id is not None]))
            if id_tup in seen:
                new_groups.append((key, tracks))
                seen.remove(id_tup)
        return new_groups

    def fill_state(self) -> None:
        groups = []
        for cols in self.COLUMN_SETS:
            for key, tracks in self.get_track_groups(cols):
                groups.append((key, tracks))
        groups = self.dedup_pages(groups)
        pages = {key: Page(tracks=tracks) for key, tracks in groups}
        self.state = DeduplicatorState(pages=pages, current_idx=0, partial_match=self.state.partial_match)
        self.state.load(self.filepath)

    def apply_changes(self) -> int:
        changed = 0
        for key, page in self.state.pages_iter:
            if not page.confirmed:
                continue
            for i, track in enumerate(page.tracks):
                new_canon = page.chosen_canons[i]
                if new_canon != page.canons[i]:
                    track.canon_id = new_canon
                    self.s.add(track)
                    changed += 1
        if changed:
            self.s.commit()
        self.state.dump(self.filepath)
        return changed

    def run(self) -> None:
        print("loading...", end="\r")
        self.fill_state()
        filtered = self.state.filtered_pages()
        self.state.current_idx = next((i for i, (_, p) in enumerate(filtered) if not p.confirmed), 0)
        print(" done!", end="\r")
        DeduplicatorUI(self).serve()


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
