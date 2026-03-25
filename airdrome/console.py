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
