from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn


console = Console()

# Output verbosity, set once from the root CLI callback: -1 quiet, 0 normal, 1 verbose.
# Business logic prints through `console` directly, so a module-level level is the simplest
# shared channel — there is one process per CLI invocation, so no cross-talk to worry about.
_verbosity = 0


def set_verbosity(level: int) -> None:
    """Set the global output verbosity: -1 quiet, 0 normal, 1 verbose."""
    global _verbosity
    _verbosity = level


def is_verbose() -> bool:
    """True when -v/--verbose was passed — callers may emit extra per-item detail."""
    return _verbosity >= 1


def detail(msg: str) -> None:
    """Print a per-item detail line, but only in verbose mode (suppressed by default).

    Use this for high-volume, low-signal output (per-file picks, per-track misses) that would
    bury the progress bars on a large library. The aggregate is still reported in the summary.
    """
    if _verbosity >= 1:
        console.print(msg)


def step(n: int, total: int, title: str) -> None:
    """Print a numbered section header for one stage of a multi-stage command.

    Frames a pipeline (e.g. `land`'s four phases) so its stacked progress bars read as
    discrete steps instead of one undifferentiated wall.
    """
    console.print(f"\n[cyan]Step {n}/{total}[/cyan] · [bold]{title}[/bold]")


def done(summary: str) -> None:
    """Print the standard command-completion line: a green check plus a one-line summary.

    Every command ends with this so success output reads the same everywhere. Reserve the
    green check for completion; announce in-progress work with plain/bold text instead.
    """
    console.print(f"[green]✓[/green] {summary}")


def make_progress(*extra_columns, transient: bool = False) -> Progress:
    """Standard progress bar. Pass extra TextColumn instances for custom fields.

    With `transient=True` the bar is cleared on completion (used by `import`, which
    prints its own persistent summary line per phase).
    """
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        *extra_columns,
        TimeElapsedColumn(),
        console=console,
        transient=transient,
    )


def make_import_progress(transient: bool = False) -> Progress:
    """Progress bar for import operations with created/updated counters."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[green]{task.fields[created]} new[/green]  [yellow]{task.fields[updated]} updated[/yellow]"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=transient,
    )
