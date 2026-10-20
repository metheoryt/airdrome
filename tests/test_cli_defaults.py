"""CLI wiring tests: lock in the user-facing defaults that differ from the engine defaults.

These guard intent, not mechanics — `organize` must default to the non-destructive *copy*, and
`dedup` with no `--set` must use `RECOMMENDED_SETS` (not the near-useless all-fields
fallback the library function keeps). The app callback's DB session is stubbed so no real DB or
migration runs; the business functions are stubbed so we can inspect exactly how they were called.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from airdrome.normalize.dedup import RECOMMENDED_SETS, AutoDedupResult
from airdrome.terminal.app import app


runner = CliRunner()


@pytest.fixture()
def stub_session(monkeypatch):
    """Replace the callback's DB session + migration with stubs so the CLI never hits Postgres.

    An *unbound real* Session, not a MagicMock: the callback attaches SQLAlchemy event
    listeners to it (the dedup mirror), which a mock silently accepts in a way that
    would never work against a real session. Unbound means any query still fails loudly,
    so a command that reaches the DB has to be stubbed too.
    """
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: None)
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: Session())


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
    spy = MagicMock(return_value=AutoDedupResult(groups=[], auto_twins=0, manual_links=0))
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", spy)
    monkeypatch.setattr("airdrome.terminal.pipeline.restore_if_empty", MagicMock(return_value=0))

    assert runner.invoke(app, ["dedup"]).exit_code == 0
    assert spy.call_args.kwargs["flag_sets"] == RECOMMENDED_SETS


def test_dedup_restores_the_mirror_before_the_batch_pass(stub_session, monkeypatch):
    """Order matters: the batch pass reads stored overrides, so seeding must precede it."""
    calls: list[str] = []
    monkeypatch.setattr(
        "airdrome.terminal.pipeline.restore_if_empty",
        lambda *a, **k: calls.append("restore") or 0,
    )
    monkeypatch.setattr(
        "airdrome.terminal.pipeline.auto_deduplicate",
        lambda *a, **k: calls.append("auto") or AutoDedupResult(groups=[], auto_twins=0, manual_links=0),
    )

    assert runner.invoke(app, ["dedup"]).exit_code == 0
    assert calls == ["restore", "auto"]


def test_dedup_explicit_set_overrides_default(stub_session, monkeypatch):
    """An explicit `--set` is parsed and used instead of the recommended default."""
    spy = MagicMock(return_value=AutoDedupResult(groups=[], auto_twins=0, manual_links=0))
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", spy)
    monkeypatch.setattr("airdrome.terminal.pipeline.restore_if_empty", MagicMock(return_value=0))

    result = runner.invoke(app, ["dedup", "--set", "artist,album"])
    assert result.exit_code == 0
    assert spy.call_args.kwargs["flag_sets"] != RECOMMENDED_SETS
    assert spy.call_args.kwargs["flag_sets"][0]["with_artist"] is True
    assert spy.call_args.kwargs["flag_sets"][0]["with_album"] is True
    assert spy.call_args.kwargs["flag_sets"][0]["with_year"] is False


def test_dedup_summary_counts_manual_superseded_and_dead(stub_session, monkeypatch):
    """The summary reports manual links, and superseded / dead decisions only when there are any."""
    result = AutoDedupResult(groups=[], auto_twins=3, manual_links=2, superseded=4, dead=1)
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", MagicMock(return_value=result))
    monkeypatch.setattr("airdrome.terminal.pipeline.restore_if_empty", MagicMock(return_value=0))

    out = " ".join(runner.invoke(app, ["dedup"]).output.split())
    assert "3 automatic + 2 manual twin(s)" in out
    assert "4 superseded decision(s)" in out
    assert "1 dead decision(s)" in out

    result.superseded = result.dead = 0
    out = " ".join(runner.invoke(app, ["dedup"]).output.split())
    assert "superseded" not in out and "dead" not in out


def test_dedup_refuses_a_legacy_mirror_without_a_traceback(stub_session, monkeypatch, tmp_path):
    """An old-format mirror stops `dedup` with a one-line red message that says how to convert it."""
    from airdrome.conf import settings

    mirror = tmp_path / "duplicates.json"
    mirror.write_text(json.dumps({"g": {"members": ["h"], "canon_hashes": [None]}}))
    monkeypatch.setattr(settings, "duplicates_filepath", mirror)
    monkeypatch.setattr(Session, "scalar", lambda self, *a, **k: None)  # the stub DB's tables are empty
    spy = MagicMock()
    monkeypatch.setattr("airdrome.terminal.pipeline.auto_deduplicate", spy)

    result = runner.invoke(app, ["dedup"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = " ".join(result.output.split())
    assert "dedup-import" in out and "scripts/migrate_dedup_groups.py" in out
    assert "Traceback" not in result.output
    spy.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_dedup_import_detects_the_format(stub_session, monkeypatch, tmp_path, legacy):
    """A version-less file goes through the legacy converter and reports dropped members."""
    from airdrome.normalize.dedup import ImportCounts
    from airdrome.normalize.dedup.legacy import Converted

    src = tmp_path / "dups.json"
    doc = (
        {"g": {"members": ["h"], "canon_hashes": [None]}}
        if legacy
        else {"version": 2, "pairs": [], "canons": []}
    )
    src.write_text(json.dumps(doc))
    counts = ImportCounts(created=1, replaced=2, kept=3)
    v2 = MagicMock(return_value=counts)
    old = MagicMock(return_value=(counts, Converted(dropped=4)))
    monkeypatch.setattr("airdrome.terminal.pipeline.import_decisions", v2)
    monkeypatch.setattr("airdrome.terminal.pipeline.import_legacy", old)

    result = runner.invoke(app, ["dedup-import", str(src)])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Imported 1 new + 2 replaced (3 kept (equal or newer in DB)) decision(s)" in out
    assert ("4 member(s) dropped (no live track)" in out) is legacy
    assert (old.called, v2.called) == (legacy, not legacy)


def test_sync_source_reconciles_one_readonly_remote_without_guard(stub_session, monkeypatch):
    """`sync apple_xml` reconciles a single read-only remote and never touches the NV guard."""
    spy = MagicMock()
    monkeypatch.setattr("airdrome.terminal.sync.reconcile", spy)
    guard = MagicMock()
    monkeypatch.setattr("airdrome.terminal.sync._guard_navidrome_stopped", guard)

    result = runner.invoke(app, ["sync", "apple_xml"])
    assert result.exit_code == 0
    guard.assert_not_called()  # no writable backend in scope

    adapters = spy.call_args.args[1]
    assert len(adapters) == 1
    assert adapters[0].writable is False


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


def test_import_batches_music_folders(stub_session, monkeypatch, tmp_path):
    """Several music folders go to one importer, so `classify` can collapse repeats across them."""
    seen: list[list] = []
    monkeypatch.setattr(
        "airdrome.ingest.sources.MusicFolderImporter.ingest", lambda self, s, kinds: seen.append(self.roots)
    )
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "song.mp3").write_bytes(b"x")

    result = runner.invoke(app, ["import", str(a), str(b)])

    assert result.exit_code == 0, result.output
    assert seen == [[a, b]]


def test_dedup_has_no_review_option(stub_session):
    """The interactive TUI is gone: `dedup --review` is a usage error."""
    result = runner.invoke(app, ["dedup", "--review"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_dedup_table_lists_each_track(session):
    """The group table has a `Canon ID` column and one row per track."""
    from airdrome.terminal.tables import dedup_table

    from factories import make_track

    a = make_track(session, "Song", "Artist")
    b = make_track(session, "Song", "Artist", "Album")
    a.date_added = b.date_added = datetime(2020, 1, 1, tzinfo=UTC)
    table = dedup_table("auto-dedup", [a, b], [None, a.id])

    assert table.row_count == 2
    assert "Canon ID" in [c.header for c in table.columns]


LEGACY_DOC = {"g": {"members": ["h"], "canon_hashes": [None]}}


@pytest.mark.parametrize("explicit", [False, True])
def test_dedup_import_refuses_the_live_legacy_mirror(stub_session, monkeypatch, tmp_path, explicit):
    """The live mirror in the old format is converted from the old DB, never imported over itself."""
    from airdrome.conf import settings
    from airdrome.normalize.dedup import ImportCounts
    from airdrome.normalize.dedup.legacy import Converted

    mirror = tmp_path / ".airdrome" / "duplicates.json"
    mirror.parent.mkdir()
    mirror.write_text(json.dumps(LEGACY_DOC))
    before = mirror.read_bytes()
    monkeypatch.setattr(settings, "duplicates_filepath", mirror)
    spy = MagicMock(return_value=(ImportCounts(), Converted(dropped=1)))
    monkeypatch.setattr("airdrome.terminal.pipeline.import_legacy", spy)

    result = runner.invoke(app, ["dedup-import", *([str(mirror)] if explicit else [])])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    out = " ".join(result.output.split())
    assert "scripts/migrate_dedup_groups.py" in out
    spy.assert_not_called()
    assert mirror.read_bytes() == before


@pytest.mark.parametrize("content", ["{not json", json.dumps({"version": 2, "pairs": [{"a": "x"}]})])
def test_dedup_import_reports_a_bad_file_without_a_traceback(stub_session, tmp_path, content):
    """An unparsable or malformed file is one red line and exit 1, not a traceback."""
    src = tmp_path / "dups.json"
    src.write_text(content)

    result = runner.invoke(app, ["dedup-import", str(src)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert result.output.strip()


def test_dedup_error_message_is_printed_literally(stub_session, monkeypatch):
    """A refusal whose text looks like Rich markup is printed as is, never parsed as a style tag."""
    monkeypatch.setattr(
        "airdrome.terminal.pipeline.restore_if_empty",
        MagicMock(side_effect=ValueError("bad [/x] tag in [bold]file[/bold]")),
    )

    result = runner.invoke(app, ["dedup"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "bad [/x] tag in [bold]file[/bold]" in result.output
