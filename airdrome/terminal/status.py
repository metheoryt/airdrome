"""The `status` command: a read-only snapshot of config health and pipeline progress.

Answers "where am I?" between stages — config sanity (DB, LIBRARY_DIR, Navidrome) plus the
counts that track the import → land → organize → dedup → push flow. It is read-only and
deliberately manages its own DB access (see the early return in the root callback) so it can
report an unreachable database instead of crashing inside the shared session setup.
"""

import re
import socket

from rich.table import Table
from sqlalchemy import ColumnElement, distinct, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.conf import settings
from airdrome.console import console
from airdrome.enums import Verdict
from airdrome.models import (
    CanonChoice,
    PairDecision,
    Playlist,
    PlaylistLink,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
    WantedVerdict,
    engine,
)
from airdrome.normalize.dedup.store import decision_counts


def _section(title: str) -> Table:
    """Print a bold section header and return a borderless label/value table to fill under it."""
    console.print(f"\n[bold]{title}[/bold]")
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    return table


def _count(session: Session, model: type[DeclarativeBase], *where: ColumnElement[bool]) -> int:
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


def _breakdown(counts: dict[str, int], prefix: str) -> list[tuple[str, int]]:
    """The `<prefix>.<name>` entries of a counts section as (name, n) pairs."""
    return [(k.removeprefix(f"{prefix}."), n) for k, n in counts.items() if k.startswith(f"{prefix}.")]


def _decision_section(s: Session) -> dict[str, int]:
    """Decision counts, all 0 while the decision tables don't exist (a DB older than the schema)."""
    inspector = inspect(s.get_bind())
    if not (
        inspector.has_table(PairDecision.__tablename__) and inspector.has_table(CanonChoice.__tablename__)
    ):
        return dict.fromkeys(("same", "different", "canons", "superseded", "dead"), 0)
    return decision_counts(s)


def pipeline_counts(s: Session) -> dict[str, dict[str, int]]:
    """Per-stage counts as data; `wanted` is omitted while the `wantedverdict` table doesn't exist yet."""
    by_provider = s.execute(select(SourceTrack.provider, func.count()).group_by(SourceTrack.provider)).all()
    by_platform = s.execute(
        select(TrackAliasScrobble.platform, func.count()).group_by(TrackAliasScrobble.platform)
    ).all()
    counts: dict[str, dict[str, int]] = {
        "imported": {
            "source_tracks": _count(s, SourceTrack),
            **{f"source_tracks.{prov}": n for prov, n in by_provider},
            "source_playlists": _count(s, SourcePlaylist, ~SourcePlaylist.folder),
            "scrobbles": _count(s, TrackAliasScrobble),
            **{f"scrobbles.{plat}": n for plat, n in by_platform},
        },
        "canonical": {
            "tracks": _count(s, Track),
            "aliases": _count(s, TrackAlias),
            "aliases_matched": _count(s, TrackAlias, TrackAlias.track_id.is_not(None)),
            "plays": _count(s, TrackPlay),
            "playlists": _count(s, Playlist),
        },
        "files": {
            "files": _count(s, TrackFile),
            "bound": _count(s, TrackFile, TrackFile.track_id.is_not(None)),
            "organized": _count(s, TrackFile, TrackFile.library_path.is_not(None)),
        },
        "dedup": {
            "twins": _count(s, Track, Track.canon_id.is_not(None)),
            "groups": s.scalar(select(func.count(distinct(Track.canon_id)))) or 0,
            **_decision_section(s),
        },
        "synced": {"playlist_links": _count(s, PlaylistLink)},
    }
    # The live DB predates the verdict table (it is recreated later); don't crash `status` there.
    if inspect(s.get_bind()).has_table(WantedVerdict.__tablename__):
        counts["wanted"] = {
            "verdict_wanted": _count(s, WantedVerdict, WantedVerdict.verdict == Verdict.WANTED),
            "verdict_skip": _count(s, WantedVerdict, WantedVerdict.verdict == Verdict.SKIP),
        }
    return counts


def _print_pipeline(session: Session) -> None:
    """Print the per-stage counts. Raises SQLAlchemyError if the schema isn't initialized."""
    c = pipeline_counts(session)

    imported = _section("Imported")
    i = c["imported"]
    imported.add_row("Source tracks", _with_breakdown(i["source_tracks"], _breakdown(i, "source_tracks")))
    imported.add_row("Source playlists", f"{i['source_playlists']:,}")
    imported.add_row("Scrobbles", _with_breakdown(i["scrobbles"], _breakdown(i, "scrobbles")))
    console.print(imported)

    landed = _section("Canonical (land)")
    k = c["canonical"]
    landed.add_row("Tracks", f"{k['tracks']:,}")
    landed.add_row("Aliases matched", _ratio(k["aliases_matched"], k["aliases"]))
    landed.add_row("Plays", f"{k['plays']:,}")
    landed.add_row("Playlists", f"{k['playlists']:,}")
    console.print(landed)

    files = _section("Files")
    f = c["files"]
    files.add_row("Bound to tracks", _ratio(f["bound"], f["files"]))
    files.add_row("Organized on disk", f"{f['organized']:,}")
    console.print(files)

    dedup = _section("Dedup")
    d = c["dedup"]
    dedup.add_row("Twins", f"{d['twins']:,} [dim]in {d['groups']:,} group(s)[/dim]")
    dedup.add_row("Decisions same", f"{d['same']:,}")
    dedup.add_row("Decisions different", f"{d['different']:,}")
    dedup.add_row("Canon choices", f"{d['canons']:,}")
    dedup.add_row("Superseded", f"{d['superseded']:,}")
    dedup.add_row("Dead", f"{d['dead']:,}")
    console.print(dedup)

    synced = _section("Synced to backends")
    synced.add_row("Playlist links", f"{c['synced']['playlist_links']:,}")
    console.print(synced)

    if "wanted" in c:
        wanted = _section("Wanted")
        wanted.add_row("Wanted", f"{c['wanted']['verdict_wanted']:,}")
        wanted.add_row("Skipped", f"{c['wanted']['verdict_skip']:,}")
        console.print(wanted)


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
