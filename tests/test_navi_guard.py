"""The guard around writing Navidrome's SQLite DB: never while Navidrome runs, and WAL folded first.

Writing that DB under a live Navidrome corrupts it, so these pin the refusal on a real listening
socket (not a stubbed probe) and the real `wal_checkpoint` on a real WAL-mode file.
"""

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from airdrome.conf import settings
from airdrome.navidrome import models as nv_models
from airdrome.terminal.app import app


runner = CliRunner()


@pytest.fixture()
def cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the callback's session and migration (see test_cli_defaults) and name a Navidrome user."""
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: None)
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: Session())
    monkeypatch.setattr(settings, "navidrome_user", "owner")


@pytest.fixture()
def writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record, in order, every call that would touch Navidrome's DB."""
    calls: list[str] = []
    for module in ("navi", "sync"):
        monkeypatch.setattr(f"airdrome.terminal.{module}.checkpoint_wal", lambda: calls.append("checkpoint"))
    monkeypatch.setattr(
        "airdrome.terminal.navi.sync_tracks_plays_to_navi", lambda *a, **k: calls.append("push")
    )
    monkeypatch.setattr("airdrome.terminal.sync.reconcile", lambda *a, **k: calls.append("reconcile"))
    monkeypatch.setattr("airdrome.terminal.sync.NavidromeAdapter", MagicMock())
    return calls


@pytest.fixture()
def navidrome_running(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real socket listening on NAVIDROME_PORT, as a running Navidrome would."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        monkeypatch.setattr(settings, "navidrome_port", srv.getsockname()[1])
        yield


@pytest.fixture()
def navidrome_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point NAVIDROME_PORT at a port nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "navidrome_port", port)


@pytest.mark.parametrize("args", [["navi", "push", "-y"], ["sync", "navidrome", "-y"], ["sync", "all", "-y"]])
def test_refuses_while_navidrome_runs(cli, writes, navidrome_running, args) -> None:
    """A listening Navidrome stops the command before anything is written, even with --yes."""
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "Navidrome is running" in result.output
    assert writes == []


def test_push_asks_and_a_no_writes_nothing(cli, writes, navidrome_stopped) -> None:
    """Without --yes the owner must confirm Navidrome is stopped; a no aborts before any write."""
    result = runner.invoke(app, ["navi", "push"], input="n\n")

    assert result.exit_code != 0
    assert writes == []


def test_push_checkpoints_before_writing(cli, writes, navidrome_stopped) -> None:
    """A confirmed push folds the WAL first, then writes."""
    result = runner.invoke(app, ["navi", "push"], input="y\n")

    assert result.exit_code == 0, result.output
    assert writes == ["checkpoint", "push"]


def test_sync_navidrome_checkpoints_before_reconciling(cli, writes, navidrome_stopped) -> None:
    """`sync navidrome` folds the WAL before the reconcile writes playlists."""
    result = runner.invoke(app, ["sync", "navidrome", "-y"])

    assert result.exit_code == 0, result.output
    assert writes == ["checkpoint", "reconcile"]


def test_checkpoint_wal_empties_the_wal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`checkpoint_wal` folds pending pages into the DB file and truncates the WAL."""
    db = tmp_path / "navidrome.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")  # keep the pages in the WAL until we fold them
    writer.execute("CREATE TABLE t (x)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    wal = tmp_path / "navidrome.db-wal"
    assert wal.stat().st_size > 0

    monkeypatch.setattr(settings, "navidrome_db_dsn", f"sqlite:///{db}")
    monkeypatch.setattr(nv_models, "_engine", None)
    try:
        nv_models.checkpoint_wal()
        assert wal.stat().st_size == 0
    finally:
        writer.close()
        if nv_models._engine is not None:
            nv_models._engine.dispose()
