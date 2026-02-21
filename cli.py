import typer
from rich.console import Console

from jellyfist.apple.ingest import ingest_library

app = typer.Typer(help="jellyfist CLI")
console = Console()


@app.command()
def ingest(filename: str, recreate: bool = typer.Option(False, "--recreate", "-r")):
    console.print("[bold green]Starting ingest...[/bold green]")
    ingest_library(filename, recreate=recreate)
    console.print("[bold green]Data ingest completed successfully.[/bold green]")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
