import typer
from rich.console import Console
from sqlmodel import SQLModel

from airdrome.models import engine

from .library import library_app
from .navidrome import navidrome_app
from .scrobble import scrobble_app


# create any missing tables
SQLModel.metadata.create_all(engine, checkfirst=True)

console = Console()

app = typer.Typer(help="Airdrome CLI")
app.add_typer(library_app, name="library")
app.add_typer(scrobble_app, name="scrobble")
app.add_typer(navidrome_app, name="navidrome")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
