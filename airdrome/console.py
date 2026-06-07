from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn


console = Console()


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
