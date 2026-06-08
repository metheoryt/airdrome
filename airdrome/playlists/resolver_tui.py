"""Interactive resolver for playlists whose remotes disagree.

Modelled on the dedup TUI (`normalize/dedup/tui.py`): a Rich `serve()` loop that shows
one conflicted playlist at a time and collects a single per-playlist strategy. The
decision surface is deliberately small — a hard conflict is rare, so the choice is
which remote wins wholesale, keep ours, or let the auto 3-way fold decide — never a
per-track editor. `serve()` returns the chosen `Decision`s for the orchestrator to apply.
"""

from collections import Counter

from rich.console import Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.models import Track

from .conflicts import Decision, PlaylistConflict, Strategy


def _signal(base: list[int], theirs: list[int], track_id: int) -> str:
    """How one remote changed a track since its base: added / removed / unchanged."""
    delta = Counter(theirs)[track_id] - Counter(base)[track_id]
    return "[green]added[/green]" if delta > 0 else "[red]removed[/red]" if delta < 0 else "[dim]·[/dim]"


def _touched_tracks(conflict: PlaylistConflict) -> list[int]:
    """Every track some remote added or removed (a superset of the hard conflicts).

    Shown in the table so `--review` of a changed-but-unconflicted playlist still
    surfaces what would move, not just the order-dependent rows.
    """
    touched: set[int] = set(conflict.conflicts)
    for st in conflict.states:
        base_c, theirs_c = Counter(st.base), Counter(st.theirs)
        touched |= {t for t in set(base_c) | set(theirs_c) if theirs_c[t] != base_c[t]}
    return sorted(touched)


class PlaylistConflictUI:
    def __init__(self, session: Session, conflicts: list[PlaylistConflict]):
        self.s = session
        self.conflicts = conflicts
        # Every conflict starts on AUTO (the deterministic default); the user overrides.
        self.decisions: dict[int, Decision] = {c.playlist_id: Decision(Strategy.AUTO) for c in conflicts}
        self.idx = 0
        self.feedback = Text()

    def _label(self, track_id: int) -> str:
        t = self.s.get(Track, track_id)
        if t is None:
            return f"#{track_id}"
        return f"{t.title} — {t.artist}" if t.artist else t.title

    def _decision_text(self, conflict: PlaylistConflict) -> Text:
        dec = self.decisions[conflict.playlist_id]
        if dec.strategy is Strategy.TAKE:
            return Text(f"take {dec.remote.value}", style="bold cyan")
        return Text(dec.strategy.value, style="bold cyan")

    def _render(self, conflict: PlaylistConflict) -> None:
        table = Table(title=f'Conflict: "{conflict.playlist_name}"')
        table.add_column("Track", style="yellow")
        for i, st in enumerate(conflict.states, start=1):
            table.add_column(f"[{i}] {st.remote.value}")
        table.add_column("ours", style="blue")

        ours_c = Counter(conflict.ours)
        for track_id in _touched_tracks(conflict):
            mark = "[bold red]⚠[/bold red] " if track_id in conflict.conflicts else ""
            cells = [f"{mark}{self._label(track_id)}"]
            cells += [_signal(st.base, st.theirs, track_id) for st in conflict.states]
            cells.append("present" if ours_c[track_id] else "absent")
            table.add_row(*cells)

        take_keys = "  ".join(
            f"[bold]{i}[/bold] take {st.remote.value}" for i, st in enumerate(conflict.states, 1)
        )
        instructions = Text.from_markup(
            f"{take_keys}\n"
            "[bold]o[/bold] keep ours    [bold]a[/bold] auto 3-way\n"
            "[bold]n[/bold] / [bold]p[/bold] next / prev    [bold]c[/bold] commit    [bold]q[/bold] abort\n"
        )

        header = Text(f"{self.idx + 1}/{len(self.conflicts)}  decision: ")
        header.append_text(self._decision_text(conflict))
        body = Group(header, self.feedback) if self.feedback else header

        console.clear()
        console.print(
            Group(
                table,
                Panel(body, title="This playlist", border_style="dim blue"),
                Panel(instructions, title="Choose", style="dim"),
            )
        )

    def serve(self) -> dict[int, Decision] | None:
        """Drive the loop. Returns the per-playlist decisions, or None if aborted."""
        if not self.conflicts:
            return {}

        while True:
            conflict = self.conflicts[self.idx]
            self._render(conflict)
            self.feedback = Text()
            cmd = Prompt.ask("Choose").strip().lower()

            if cmd == "q":
                return None
            if cmd == "c":
                return self.decisions
            if cmd == "n":
                self.idx = (self.idx + 1) % len(self.conflicts)
            elif cmd == "p":
                self.idx = (self.idx - 1) % len(self.conflicts)
            elif cmd == "o":
                self.decisions[conflict.playlist_id] = Decision(Strategy.OURS)
            elif cmd == "a":
                self.decisions[conflict.playlist_id] = Decision(Strategy.AUTO)
            elif cmd.isdigit() and 1 <= int(cmd) <= len(conflict.states):
                remote = conflict.states[int(cmd) - 1].remote
                self.decisions[conflict.playlist_id] = Decision(Strategy.TAKE, remote)
            else:
                self.feedback = Text(f"Unrecognized: {cmd!r}", style="bold red")
