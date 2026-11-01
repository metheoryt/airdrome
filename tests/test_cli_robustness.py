"""The root callback's failure modes: help without the DB, an unknown alembic stamp, a failed commit.

Each guards a way the CLI used to fail silently or against the wrong thing: `<cmd> --help` migrated the
live DB before printing, an unknown revision ended in alembic's traceback, and a commit that failed
was rolled back with no message and exit 0 — after the command had printed its summary.
"""

from collections.abc import Iterator

import pytest
import typer
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from airdrome import migrations
from airdrome.conf import settings
from airdrome.terminal.app import app


runner = CliRunner()

SCRATCH_DB = "airdrome_unknownrev_test"


def _command_paths(cmd: object, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every command and group under `cmd`, as the argv that names it."""
    if prefix:
        yield prefix
    # Typer vendors its own Click, so the group type is not `click.Group`; a group is what has commands.
    for name, sub in getattr(cmd, "commands", {}).items():
        yield from _command_paths(sub, (*prefix, name))


ALL_COMMANDS = sorted(_command_paths(typer.main.get_command(app)))


def _forbidden(*_a: object, **_k: object) -> None:
    """Stands in for anything that would reach the DB; a help request must never call it."""
    raise AssertionError("reached the DB")


@pytest.fixture()
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make migrating or opening a session fail the test."""
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", _forbidden)
    monkeypatch.setattr("airdrome.terminal.app.Session", _forbidden)


def test_walk_finds_nested_commands() -> None:
    """The parametrization below covers nested commands, so a new one is checked without listing it."""
    assert ("maint", "enrich") in ALL_COMMANDS
    assert ("sync", "all") in ALL_COMMANDS
    assert ("land",) in ALL_COMMANDS


@pytest.mark.parametrize("path", ALL_COMMANDS, ids=" ".join)
def test_help_never_reaches_the_db(no_db: None, path: tuple[str, ...]) -> None:
    """`<cmd> --help` prints help and exits 0 without migrating or opening a session."""
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_help_after_double_dash_is_an_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--help` after `--` is a value (here a path), so the command runs and does reach the DB."""
    migrated: list[int] = []
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: migrated.append(1))
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: Session())
    result = runner.invoke(app, ["import", "--", "--help"])
    assert result.exit_code != 0  # no such path
    assert migrated == [1]


@pytest.mark.parametrize(("state", "upgrades"), [("head", 0), ("behind", 1)])
def test_upgrade_runs_only_when_behind(monkeypatch: pytest.MonkeyPatch, state: str, upgrades: int) -> None:
    """At head nothing runs; behind (a fresh DB included) gets `alembic upgrade head`."""
    calls: list[str] = []
    monkeypatch.setattr(migrations, "schema_state", lambda: state)
    monkeypatch.setattr(migrations.command, "upgrade", lambda _cfg, rev: calls.append(rev))
    migrations.upgrade_to_head()
    assert calls == ["head"] * upgrades


@pytest.fixture()
def unknown_rev_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A throwaway DB stamped with a revision no script knows; `settings.db_dsn` points at it."""
    base, _ = str(settings.db_dsn).rsplit("/", 1)
    dsn = f"{base}/{SCRATCH_DB}"
    admin = create_engine(str(settings.db_dsn), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    scratch = create_engine(dsn)
    with scratch.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('bc7f40a34db0')"))
    scratch.dispose()
    monkeypatch.setattr(settings, "db_dsn", dsn)
    try:
        yield dsn
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        admin.dispose()


def test_unknown_revision_points_at_the_readme(unknown_rev_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command on a DB stamped with an unknown revision exits 1 with the README pointer, no traceback.

    `alembic upgrade` itself is forbidden: the refusal must come before it, from `schema_state`.
    """
    monkeypatch.setattr(migrations.command, "upgrade", _forbidden)
    monkeypatch.setattr("airdrome.terminal.app.Session", _forbidden)
    result = runner.invoke(app, ["land"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    out = " ".join(result.output.split())
    assert "Recreating the database" in out
    assert "Traceback" not in result.output


class _FailingCommitSession(Session):
    """A real (unbound) session whose commit fails the way a dropped connection does."""

    rolled_back = False

    def commit(self) -> None:
        """Fail like a connection lost at COMMIT."""
        raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))

    def rollback(self) -> None:
        """Record that the failed transaction was rolled back."""
        type(self).rolled_back = True
        super().rollback()


def test_failed_commit_is_reported_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit failing in `_finalize` rolls back, says nothing was saved, and exits 1."""
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: None)
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: _FailingCommitSession())
    monkeypatch.setattr("airdrome.terminal.pipeline.organize_library", lambda *a, **k: None)
    _FailingCommitSession.rolled_back = False

    result = runner.invoke(app, ["organize"])

    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "nothing was saved" in out
    assert "server closed the connection" in out
    assert _FailingCommitSession.rolled_back
