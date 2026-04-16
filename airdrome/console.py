from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn


console = Console()


def make_progress(*extra_columns) -> Progress:
    """Standard progress bar. Pass extra TextColumn instances for custom fields."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        *extra_columns,
        TimeElapsedColumn(),
        console=console,
    )


def make_import_progress() -> Progress:
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
    )
