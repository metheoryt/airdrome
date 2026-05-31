from rich.console import Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from airdrome.console import console
from airdrome.enums import Source
from airdrome.models import Track

from .manual import Deduplicator, Page
from .persistence import save_confirmed_groups


INSTRUCTION_TEXT = Text.from_markup(
    "[bold]1[/bold] - mark 1 as canon, others as twins\n"
    "[bold]1 2 3[/bold] - mark 1 as canon of 2 and 3\n"
    "[bold]Enter[/bold] - confirm current group\n"
    "[bold]r[/bold] - reset choices for this group\n"
    "[bold]a[/bold] / [bold]d[/bold] - previous / next group\n"
    "[bold]m[/bold] - cycle mode: resolved all / unconfirmed / confirmed / auto-resolved\n"
    "[bold]c[/bold] - commit changes\n"
    "[bold]q[/bold] - exit\n"
)


class DeduplicatorUI:
    def __init__(self, deduplicator: Deduplicator):
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
        xml_n = sum(1 for st in t.source_tracks if st.provider == Source.APPLE_XML)
        ams_n = sum(1 for st in t.source_tracks if st.provider == Source.APPLE_MS)
        return {
            "ID": str(t.id),
            "Title": t.title,
            "Artist": t.artist or "",
            "Album artist": t.album_artist or "",
            "Album": t.album or "",
            "Track #": str(t.track_n) if t.track_n else "",
            "Disc #": str(t.disc_n) if t.disc_n else "",
            "Year": str(t.year) or "",
            "Comp": "✅" if t.compilation else "",
            "Time": f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "",
            "Files": "\n".join([f.duration_str or "--:--" for f in t.files]) if len(t.files) else "",
            "Date added": t.date_added.strftime("%Y-%m-%d"),
            "❤️": "❤️" if t.loved else "",
            "Album ❤️": "❤️" if t.album_loved else "",
            "XML": str(xml_n) if xml_n else "",
            "AMS": str(ams_n) if ams_n else "",
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
                    save_confirmed_groups(self.dedup.s, self.state.pages)
                    self.dedup.s.commit()
                    console.print("[green]Exited.[/green]")
                    return
                elif cmd == "m":
                    self.state.switch_mode()
                elif cmd == "c":
                    n = self.dedup.apply_changes()
                    self.dedup.s.commit()
                    self.feedback_text = Text(
                        f"{n} change(s) committed. Saved to DB",
                        style="bold green",
                    )
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
                self.dedup.s.commit()
                self.feedback_text = Text(
                    f"{n} change(s) committed. Saved to DB",
                    style="bold green",
                )
            elif action == "mode":
                self.state.switch_mode()
                self.feedback_text = Text()
            elif action == "exit":
                save_confirmed_groups(self.dedup.s, self.state.pages)
                self.dedup.s.commit()
                console.print("[green]Exited.[/green]")
                return
