"""The `status` command: a read-only snapshot of config health and pipeline progress.

Answers "where am I?" between stages — config sanity (DB, LIBRARY_DIR, Navidrome) plus the
counts that track the import → land → organize → dedup → push flow. It is read-only and
deliberately manages its own DB access (see the early return in the root callback) so it can
report an unreachable database instead of crashing inside the shared session setup.
"""

import re
import socket

from rich.table import Table
from sqlalchemy import distinct, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.conf import settings
from airdrome.console import console
from airdrome.models import (
    DedupGroup,
    Playlist,
    PlaylistLink,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
    engine,
)


def _section(title: str) -> Table:
    """Print a bold section header and return a borderless label/value table to fill under it."""
    console.print(f"\n[bold]{title}[/bold]")
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    return table


def _count(session: Session, model, *where) -> int:
    """COUNT(*) over `model`, optionally filtered, returning 0 instead of None."""
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return session.scalar(stmt) or 0


def _ratio(part: int, total: int) -> str:
    """`part / total (pct%)` — the percentage is dropped when total is 0."""
    pct = f" [dim]({part / total:.0%})[/dim]" if total else ""
    return f"{part:,} / {total:,}{pct}"


def _with_breakdown(total: int, rows: list) -> str:
    """A total with a dim per-key breakdown, e.g. `12,345 (apple_xml 6,000, spotify 6,345)`."""
    if not total:
        return "[dim]0[/dim]"
    bits = ", ".join(f"{name} {n:,}" for name, n in rows)
    return f"{total:,} [dim]({bits})[/dim]"


def _dsn_summary() -> str:
    """The configured Postgres DSN with the user:password stripped (never echo credentials)."""
    return re.sub(r"//[^@/]*@", "//", str(settings.db_dsn))


def _navidrome_running(port: int) -> bool:
    """True if something is listening on localhost:port (i.e. Navidrome is up, push unsafe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("localhost", port)) == 0


def _navidrome_summary() -> str:
    if not settings.navidrome_db_dsn:
        return "[dim]not configured[/dim]"
    user = f"user={settings.navidrome_user}" if settings.navidrome_user else "[yellow]no user[/yellow]"
    if _navidrome_running(settings.navidrome_port):
        state = f"[red]running on :{settings.navidrome_port}[/red]"
    else:
        state = "[green]stopped[/green]"
    return f"{user}  {state}"


def _library_state() -> str:
    d = settings.library_dir
    if not d.exists():
        return f"[yellow]{d}[/yellow] [dim](missing)[/dim]"
    state = "has files" if any(d.iterdir()) else "[dim]empty[/dim]"
    return f"{d} [dim]({state})[/dim]"


def _print_pipeline(session: Session) -> None:
    """Print the per-stage counts. Raises SQLAlchemyError if the schema isn't initialized."""
    imported = _section("Imported")
    by_provider = session.execute(
        select(SourceTrack.provider, func.count()).group_by(SourceTrack.provider)
    ).all()
    imported.add_row("Source tracks", _with_breakdown(_count(session, SourceTrack), by_provider))
    imported.add_row("Source playlists", f"{_count(session, SourcePlaylist, ~SourcePlaylist.folder):,}")
    by_platform = session.execute(
        select(TrackAliasScrobble.platform, func.count()).group_by(TrackAliasScrobble.platform)
    ).all()
    imported.add_row("Scrobbles", _with_breakdown(_count(session, TrackAliasScrobble), by_platform))
    console.print(imported)

    landed = _section("Canonical (land)")
    landed.add_row("Tracks", f"{_count(session, Track):,}")
    landed.add_row(
        "Aliases matched",
        _ratio(_count(session, TrackAlias, TrackAlias.track_id.is_not(None)), _count(session, TrackAlias)),
    )
    landed.add_row("Plays", f"{_count(session, TrackPlay):,}")
    landed.add_row("Playlists", f"{_count(session, Playlist):,}")
    console.print(landed)

    files = _section("Files")
    files.add_row(
        "Bound to tracks",
        _ratio(_count(session, TrackFile, TrackFile.track_id.is_not(None)), _count(session, TrackFile)),
    )
    files.add_row(
        "Organized on disk",
        f"{_count(session, TrackFile, TrackFile.library_path.is_not(None)):,}",
    )
    console.print(files)

    dedup = _section("Dedup")
    twins = _count(session, Track, Track.canon_id.is_not(None))
    groups = session.scalar(select(func.count(distinct(Track.canon_id)))) or 0
    dedup.add_row("Twins", f"{twins:,} [dim]in {groups:,} group(s)[/dim]")
    dedup.add_row("Confirmed groups", f"{_count(session, DedupGroup):,}")
    console.print(dedup)

    synced = _section("Synced to backends")
    synced.add_row("Playlist links", f"{_count(session, PlaylistLink):,}")
    console.print(synced)


def status() -> None:
    """Show a read-only snapshot of configuration and pipeline progress.

    Safe to run anytime: it never writes, doesn't apply migrations, and reports an
    unreachable database rather than failing on it.
    """
    env = _section("Environment")
    try:
        session = Session(engine)
        session.execute(select(1))
        env.add_row("Database", f"[green]connected[/green] [dim]{_dsn_summary()}[/dim]")
        db_ok = True
    except SQLAlchemyError as exc:
        env.add_row("Database", f"[red]unreachable[/red] [dim]{type(exc).__name__}[/dim]")
        db_ok = False
    env.add_row("Library dir", _library_state())
    env.add_row("Navidrome", _navidrome_summary())
    console.print(env)

    if not db_ok:
        console.print("\n[yellow]Skipping pipeline counts — database is unreachable.[/yellow]")
        return

    try:
        _print_pipeline(session)
    except SQLAlchemyError as exc:
        console.print(
            f"\n[yellow]Schema not initialized ({type(exc).__name__}). "
            "Run any write command first to apply migrations.[/yellow]"
        )
    finally:
        session.close()
