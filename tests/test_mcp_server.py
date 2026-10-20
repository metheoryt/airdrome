"""Tests for the FastMCP server: the tool surface, per-call commits, and that the token never hits a log."""

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import airdrome.terminal.app as app_module
import airdrome.terminal.mcp as mcp_module
from airdrome.cloud.sources import SourceTrack
from airdrome.conf import settings
from airdrome.enums import Source
from airdrome.library import card
from airdrome.mcpserver.server import build_server, seed_decisions
from airdrome.mcpserver.session import tool_session
from airdrome.models import AirdromeBase, PairDecision, Track, TrackPlay
from airdrome.normalize.dedup.decisions import key_text, track_key
from airdrome.normalize.dedup.mirror import write_mirror
from airdrome.normalize.dedup.store import export_decisions
from airdrome.terminal.app import app

from factories import add_pair, make_track


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DB = "airdrome_mcptest"
TOOLS = {
    "wanted_library",
    "wanted_scrobbled",
    "track_card",
    "unmatched_aliases",
    "library_status",
    "search_music",
    "set_verdict",
    "clear_verdict",
    "mark_same_track",
    "mark_different_tracks",
    "bind_aliases",
    "reject_aliases",
}


@pytest.fixture()
def committed(test_engine, tmp_path, monkeypatch):
    """The test engine for tools that really commit; mirror under tmp_path, every table emptied after."""
    assert test_engine.url.database.endswith("_test")  # never clean anything but the test DB
    monkeypatch.setattr(settings, "duplicates_filepath", tmp_path / "duplicates.json")
    yield test_engine
    names = ", ".join(f'"{t.name}"' for t in AirdromeBase.metadata.sorted_tables)
    with test_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def _call(engine, name: str, **args) -> dict:
    """Call one tool through the in-memory client and return its structured result."""

    async def go() -> dict:
        async with Client(build_server(engine)) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


def _library_track(engine, title: str) -> int:
    """Commit a file-less library track (an APPLE_XML source) and return its id."""
    with Session(engine) as s:
        t = Track(title=title, artist="Band")
        s.add(t)
        s.flush()
        s.add(SourceTrack(provider=Source.APPLE_XML, source_id=title, track_id=t.id, title=title))
        s.commit()
        return t.id


def test_tools_listed(committed):
    async def go() -> set[str]:
        async with Client(build_server(committed)) as c:
            return {t.name for t in await c.list_tools()}

    assert asyncio.run(go()) == TOOLS


def test_search_music_finds_a_track(committed, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "search_dirs", [])
    monkeypatch.setattr(settings, "library_dir", tmp_path)
    tid = _library_track(committed, "Take On Me")
    res = _call(committed, "search_music", query="take on me")
    assert [r["track_id"] for r in res["tracks"]["rows"]] == [tid]
    with pytest.raises(ToolError, match="no words"):
        _call(committed, "search_music", query="!!")


def test_set_verdict_roundtrip(committed):
    tid = _library_track(committed, "Song")
    _call(committed, "set_verdict", verdict="wanted", track_id=tid)
    page = _call(committed, "wanted_library")
    [row] = [r for r in page["rows"] if r["track_id"] == tid]
    assert row["verdict"] == "wanted"
    assert page["next_cursor"] is None


def test_wanted_rows_are_json_safe(committed):
    """Datetimes and enums come out as plain strings."""
    tid = _library_track(committed, "Song")
    with Session(committed) as s:
        s.add(TrackPlay(track_id=tid, played_at=datetime(2022, 1, 1, tzinfo=UTC), platform=Source.LASTFM))
        s.commit()
    _call(committed, "set_verdict", verdict="skip", track_id=tid, note="no")
    [row] = _call(committed, "wanted_library", verdict="skip")["rows"]
    assert row["verdict"] == "skip"
    assert row["first_played"].startswith("2022-01-01")


def test_set_verdict_needs_exactly_one_target(committed):
    tid = _library_track(committed, "Song")
    with pytest.raises(ToolError, match="exactly one"):
        _call(committed, "set_verdict", verdict="wanted")
    with pytest.raises(ToolError, match="exactly one"):
        _call(committed, "set_verdict", verdict="wanted", track_id=tid, alias_ids=[1])


def test_tool_error_rolls_back(committed):
    tid = _library_track(committed, "Song")
    with pytest.raises(ToolError, match="itself"):
        _call(committed, "mark_same_track", track_id_a=tid, track_id_b=tid)
    with Session(committed) as s:
        assert s.scalar(select(func.count()).select_from(PairDecision)) == 0


def test_tool_session_rolls_back_flushed_work_on_error(committed):
    """A failure after a flush leaves nothing behind."""
    with pytest.raises(RuntimeError), tool_session(committed) as s:
        s.add(Track(title="ghost"))
        s.flush()
        raise RuntimeError("boom")
    with Session(committed) as s:
        assert s.scalar(select(func.count()).select_from(Track)) == 0


def test_mark_same_returns_card_and_writes_mirror(committed, tmp_path):
    a = _library_track(committed, "Song")
    b = _library_track(committed, "Song (Remaster)")
    card = _call(committed, "mark_same_track", track_id_a=a, track_id_b=b)
    assert {m["id"] for m in card["members"]} == {a, b}
    assert (tmp_path / "duplicates.json").exists()  # the mirror follows the tool's commit


def test_unknown_track_is_a_tool_error(committed):
    with pytest.raises(ToolError, match="unknown track id"):
        _call(committed, "track_card", track_id=999_999)


def test_unexpected_errors_are_masked(committed, monkeypatch):
    """A non-ValueError never ships its message (SQL, params) to the client."""

    def boom(s, track_id):
        raise RuntimeError("SELECT secret FROM track")

    monkeypatch.setattr(card, "track_card", boom)
    with pytest.raises(ToolError) as exc:
        _call(committed, "track_card", track_id=1)
    assert "secret" not in str(exc.value)


def _ends(s) -> set[frozenset[str]]:
    """Every stored pair decision, as the set of its two key texts."""
    return {frozenset((r.key_a, r.key_b)) for r in s.scalars(select(PairDecision))}


def _file_ends() -> set[frozenset[str]]:
    """Every pair decision in the mirror file, as the set of its two key texts."""
    data = json.loads(settings.duplicates_file.read_text(encoding="utf-8"))
    return {frozenset((key_text(tuple(p["a"])), key_text(tuple(p["b"])))) for p in data["pairs"]}


def test_edit_after_rebuild_keeps_mirrored_decisions(committed):
    """Startup seeds empty decision tables from the mirror, so the first edit can't overwrite the file."""
    with Session(committed) as s:
        t1, t2 = make_track(s, "Hand", "Band", "A"), make_track(s, "Hand", "Band", "B")
        t3, t4 = make_track(s, "Other", "Band", "A"), make_track(s, "Other", "Band", "B")
        add_pair(s, t1, t2)
        write_mirror(export_decisions(s), settings.duplicates_file)
        s.query(PairDecision).delete()  # the rebuilt DB: no decisions, mirror intact
        s.commit()
        handmade = frozenset((key_text(track_key(t1)), key_text(track_key(t2))))
        edited = frozenset((key_text(track_key(t3)), key_text(track_key(t4))))
        a, b = t3.id, t4.id

    assert seed_decisions(committed) == 1
    _call(committed, "mark_different_tracks", track_id_a=a, track_id_b=b)

    with Session(committed) as s:
        assert _ends(s) == {handmade, edited}
    assert _file_ends() == {handmade, edited}


def test_rebuild_while_server_runs_keeps_mirrored_decisions(committed):
    """A DB rebuilt after startup: the next edit re-seeds the tables before it snapshots them."""
    with Session(committed) as s:
        ts = [make_track(s, f"T{i}") for i in range(4)]
        add_pair(s, ts[0], ts[1])
        write_mirror(export_decisions(s), settings.duplicates_file)
        s.commit()
        ids = [t.id for t in ts]
        keys = [key_text(track_key(t)) for t in ts]
    server = build_server(committed)  # built once: the rebuild happens while it serves
    with committed.begin() as conn:
        conn.execute(text("TRUNCATE pairdecision, canonchoice RESTART IDENTITY CASCADE"))

    async def go() -> None:
        """Make one edit through the already-running server."""
        async with Client(server) as c:
            await c.call_tool("mark_same_track", {"track_id_a": ids[2], "track_id_b": ids[3]})

    asyncio.run(go())

    expected = {frozenset(keys[:2]), frozenset(keys[2:])}
    with Session(committed) as s:
        assert _ends(s) == expected
    assert _file_ends() == expected


def test_seed_is_a_noop_when_decisions_exist(committed):
    with Session(committed) as s:
        t1, t2 = make_track(s, "Hand", "Band", "A"), make_track(s, "Hand", "Band", "B")
        add_pair(s, t1, t2)
        write_mirror(export_decisions(s), settings.duplicates_file)
        s.commit()
    assert seed_decisions(committed) == 0


def test_seed_refuses_a_legacy_mirror(committed):
    """The server must not start on an old-format file it would later overwrite."""
    settings.duplicates_file.write_text(json.dumps({"g": {"members": ["h"], "canon_hashes": [None]}}))
    with pytest.raises(ValueError, match="dedup-import"):
        seed_decisions(committed)


@pytest.mark.parametrize(
    ("state", "message"),
    [("behind", "alembic upgrade head"), ("unknown", "predates the current schema")],
)
def test_mcp_refuses_off_head_without_migrating(monkeypatch, state, message):
    monkeypatch.setattr(settings, "airdrome_mcp_token", SecretStr("x" * 32))
    monkeypatch.setattr(mcp_module, "schema_state", lambda: state)
    upgrades: list[int] = []
    monkeypatch.setattr(app_module, "upgrade_to_head", lambda: upgrades.append(1))
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert message in out
    assert ("migrate_dedup_groups.py" in out) is (state == "unknown")
    assert upgrades == []


def test_mcp_refuses_without_token(monkeypatch):
    monkeypatch.setattr(settings, "airdrome_mcp_token", None)
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 1
    assert "AIRDROME_MCP_TOKEN" in result.output


def _free_port() -> int:
    """A port nothing listens on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _status(req: urllib.request.Request) -> int:
    """The HTTP status of one request, error statuses included."""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture()
def scratch_dsn():
    """A throwaway DB at alembic head (migrated in a subprocess so only DB_DSN decides the target)."""
    base, _ = str(settings.db_dsn).rsplit("/", 1)
    dsn = f"{base}/{SCRATCH_DB}"
    assert dsn.rsplit("/", 1)[1] == SCRATCH_DB
    admin = create_engine(str(settings.db_dsn), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    try:
        subprocess.run(
            [sys.executable, "-c", "from airdrome.migrations import upgrade_to_head; upgrade_to_head()"],
            cwd=REPO_ROOT,
            env={**os.environ, "DB_DSN": dsn},
            check=True,
            capture_output=True,
        )
        yield dsn
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        admin.dispose()


def test_server_logs_never_contain_token(scratch_dsn, tmp_path):
    token = secrets.token_urlsafe(32)
    port = _free_port()
    env = {
        **os.environ,
        "DB_DSN": scratch_dsn,
        "AIRDROME_MCP_TOKEN": token,
        "AIRDROME_MCP_PORT": str(port),
        "DUPLICATES_FILEPATH": str(tmp_path / "duplicates.json"),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from airdrome.terminal.app import app; app()", "mcp"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, proc.communicate()[0].decode()
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "server never started listening"
                time.sleep(0.2)
        base = f"http://127.0.0.1:{port}"
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        ok = _status(
            urllib.request.Request(
                f"{base}/mcp/{token}",
                data=json.dumps(init).encode(),
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            )
        )
        wrong = _status(urllib.request.Request(f"{base}/mcp/wrong"))
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            out = proc.communicate(timeout=15)[0].decode()
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0].decode()

    assert "airdrome MCP on" in out  # the output really was captured
    assert token not in out
    assert ok == 200
    assert wrong == 404


def test_mcp_reports_a_legacy_mirror_in_one_line(monkeypatch):
    """A seed refusal (an old-format mirror) is a red line and exit 1, not a traceback."""
    monkeypatch.setattr(settings, "airdrome_mcp_token", SecretStr("x" * 32))
    monkeypatch.setattr(mcp_module, "schema_state", lambda: "head")
    monkeypatch.setattr(
        mcp_module, "seed_decisions", lambda engine: (_ for _ in ()).throw(ValueError("old [format] mirror"))
    )
    served: list[int] = []
    monkeypatch.setattr(mcp_module.uvicorn, "run", lambda *a, **k: served.append(1))

    result = CliRunner().invoke(app, ["mcp"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "old [format] mirror" in result.output
    assert served == []
