import typer
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.migrations import upgrade_to_head
from airdrome.models import engine

from .apple import apple_app
from .library import library_app
from .navidrome import navidrome_app
from .scrobble import scrobble_app
from .state import AppState


upgrade_to_head()

app = typer.Typer(help="Airdrome CLI")
app.add_typer(apple_app, name="apple")
app.add_typer(library_app, name="library")
app.add_typer(scrobble_app, name="scrobble")
app.add_typer(navidrome_app, name="navidrome")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        return
    session = ctx.with_resource(Session(engine))
    ctx.obj = AppState(session=session, dry_run=False)

    def _finalize():
        if ctx.obj.dry_run:
            session.rollback()
            console.print("[dim]dry run — no changes committed[/dim]")
        else:
            try:
                session.commit()
            except Exception:
                session.rollback()

    ctx.call_on_close(_finalize)


@apple_app.callback(invoke_without_command=True)
@library_app.callback(invoke_without_command=True)
@scrobble_app.callback(invoke_without_command=True)
@navidrome_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
