"""Shared Typer option constants reused across commands.

A single definition per common flag keeps help text and short aliases identical everywhere
(Typer reuses one Option sentinel safely — every command that exposes the flag points here).
"""

import typer


DRY_RUN = typer.Option(False, "--dry-run", "-n", help="Roll back all changes after execution.")
YES = typer.Option(False, "--yes", "-y", help="Skip the Navidrome-stopped confirmation.")
REVIEW = typer.Option(
    False, "--review", "-r", help="Open the resolver for every changed playlist, not just conflicts."
)
