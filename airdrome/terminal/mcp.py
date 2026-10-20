"""`airdrome mcp` — serve the MCP tools on localhost under the secret token path."""

import typer
import uvicorn
from rich.markup import escape

from airdrome.conf import settings
from airdrome.console import console
from airdrome.mcpserver.server import build_server, seed_decisions
from airdrome.migrations import schema_state
from airdrome.models import engine


def mcp() -> None:
    """Serve the Airdrome MCP tools on http://127.0.0.1:<port>/mcp/<token> (never migrates)."""
    if settings.airdrome_mcp_token is None:
        console.print("[red]set AIRDROME_MCP_TOKEN in .env[/red]")
        raise typer.Exit(1)
    state = schema_state()
    if state == "unknown":
        console.print(
            "[red]the DB predates the current schema; recreate it (see *Recreating the database* in "
            'README) — first keep a pg_dump of this DB and, if the mirror has no "version", convert '
            "its dedup groups with scripts/migrate_dedup_groups.py[/red]"
        )
        raise typer.Exit(1)
    if state == "behind":
        console.print("[red]schema is not at head — run `uv run alembic upgrade head` first[/red]")
        raise typer.Exit(1)
    # A data seed, not a migration: a rebuilt DB shows its manual decisions from the first read. Every
    # edit re-seeds too (`write` in server.py), which covers a rebuild while the server runs.
    try:
        seed_decisions(engine)
    except ValueError as exc:  # an old-format or unparsable mirror: nothing was seeded or written
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    token = settings.airdrome_mcp_token.get_secret_value()
    port = settings.airdrome_mcp_port
    console.print(f"airdrome MCP on http://127.0.0.1:{port}/mcp/<redacted>")
    # Not `FastMCP.run`: it logs "Starting MCP server ... on http://host:port/<path>" whether or not the
    # banner is shown, and the path is the token. Uvicorn on the bare app logs only host and port; its
    # access log (which prints every request path) is off.
    app = build_server(engine).http_app(path=f"/mcp/{token}")
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
