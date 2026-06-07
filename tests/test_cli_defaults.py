"""CLI wiring tests: lock in the user-facing defaults that differ from the engine defaults.

These guard intent, not mechanics — `organize` must default to the non-destructive *copy*, and
`dedup` with no `--set` must use `RECOMMENDED_SETS` (not the near-useless all-fields
fallback the library function keeps). The app callback's DB session is stubbed so no real DB or
migration runs; the business functions are stubbed so we can inspect exactly how they were called.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from airdrome.normalize.dedup import RECOMMENDED_SETS, AutoDedupResult
from airdrome.terminal.app import app


runner = CliRunner()


@pytest.fixture()
def stub_session(monkeypatch):
    """Replace the callback's DB session + migration with stubs so the CLI never hits Postgres."""
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: None)
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: MagicMock())


def test_organize_defaults_to_copy(stub_session, monkeypatch):
    """`organize` with no flag copies (safe default); `--move` flips it to a move."""
    spy = MagicMock()
    monkeypatch.setattr("airdrome.terminal.pipeline.organize_library", spy)

    assert runner.invoke(app, ["organize"]).exit_code == 0
    assert spy.call_args.kwargs["copy"] is True

    spy.reset_mock()
    assert runner.invoke(app, ["organize", "--move"]).exit_code == 0
    assert spy.call_args.kwargs["copy"] is False


def test_dedup_defaults_to_recommended_sets(stub_session, monkeypatch):
    """`dedup` with no `--set` uses RECOMMENDED_SETS."""
    spy = MagicMock(return_value=AutoDedupResult(groups=[], auto_twins=0, manual_changes=0))
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", spy)

    assert runner.invoke(app, ["dedup"]).exit_code == 0
    assert spy.call_args.kwargs["flag_sets"] == RECOMMENDED_SETS


def test_dedup_explicit_set_overrides_default(stub_session, monkeypatch):
    """An explicit `--set` is parsed and used instead of the recommended default."""
    spy = MagicMock(return_value=AutoDedupResult(groups=[], auto_twins=0, manual_changes=0))
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", spy)

    result = runner.invoke(app, ["dedup", "--set", "artist,album"])
    assert result.exit_code == 0
    assert spy.call_args.kwargs["flag_sets"] != RECOMMENDED_SETS
    assert spy.call_args.kwargs["flag_sets"][0]["with_artist"] is True
    assert spy.call_args.kwargs["flag_sets"][0]["with_album"] is True
    assert spy.call_args.kwargs["flag_sets"][0]["with_year"] is False


def test_status_reports_unreachable_db(monkeypatch):
    """`status` reports an unreachable DB instead of crashing — and never runs migrations.

    This is the whole point of the command's defensive, self-managed session (it bypasses the
    root callback's session setup), so it must survive a database that other commands can't.
    """
    migrate = MagicMock()
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", migrate)

    broken = MagicMock()
    broken.execute.side_effect = SQLAlchemyError("connection refused")
    monkeypatch.setattr("airdrome.terminal.status.Session", lambda *a, **k: broken)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "unreachable" in result.stdout
    migrate.assert_not_called()
