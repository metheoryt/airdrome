"""`--dry-run`/`-n` writes nothing, on every write command, run through the real CLI callback.

Each case runs the command twice on the same seed: with `-n` the DB (and the dedup mirror) must be
unchanged, and without it the same command must change it — so a case cannot pass because the
command had nothing to do. The callback's session is the test's savepoint session, so a commit the
command makes on its own would survive the dry run here exactly as it would in production.
"""

import json
import socket
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from airdrome.cloud.acoustid import IdentifyStats
from airdrome.cloud.apple.xml_library import do_import_playlists, do_import_tracks
from airdrome.cloud.sources import SourcePlaylist, SourcePlaylistTrack, SourceTrack
from airdrome.conf import settings
from airdrome.enums import Source
from airdrome.library.unify import unify_source_playlists, unify_source_tracks
from airdrome.models import PairDecision, Playlist, PlaylistTrack, Track, TrackFile
from airdrome.navidrome import models as nv_models
from airdrome.navidrome.models import NavidromeBase, Playlist as NVPlaylist
from airdrome.terminal.app import app
from tests.factories import make_track
from tests.test_apple_ingest import _playlist_data, _track_data
from tests.test_navidrome_sync import LIB_PATH, _seed_navidrome


runner = CliRunner()


@pytest.fixture()
def cli(session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., str]:
    """Run the CLI on the test session; return its output, failing on a non-zero exit."""
    monkeypatch.setattr("airdrome.terminal.app.upgrade_to_head", lambda: None)
    monkeypatch.setattr("airdrome.terminal.app.Session", lambda *a, **k: session)
    monkeypatch.setattr(settings, "duplicates_filepath", tmp_path / "mirror" / "duplicates.json")

    def run(*args: str) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    return run


def _count(session: Session, model: type) -> int:
    """Rows of `model` as the DB holds them now."""
    return session.scalar(select(func.count()).select_from(model))


def _assert_dry_then_real(
    cli: Callable[..., str], session: Session, args: list[str], probe: Callable[[], object]
):
    """`args -n` leaves `probe()` and the mirror as they were; `args` alone changes `probe()`."""
    # Release the seed into the outer test transaction: the dry run's rollback would undo it otherwise.
    session.commit()
    before = probe()
    out = cli(*args, "-n")
    assert "nothing was committed" in out
    assert probe() == before
    assert not settings.duplicates_file.exists()

    cli(*args)
    assert probe() != before


def test_import_dry_run(cli, session, tmp_path, make_wav) -> None:
    """`import <folder> -n` stores no file."""
    folder = tmp_path / "music"
    folder.mkdir()
    make_wav(folder / "a.wav", seed=1, seconds=3)

    _assert_dry_then_real(cli, session, ["import", str(folder)], lambda: _count(session, TrackFile))


def test_land_dry_run(cli, session) -> None:
    """`land -n` creates no canonical track from an imported source track."""
    data = _track_data(name="Dry Song")
    do_import_tracks(session, {str(data["Track ID"]): data})
    session.flush()

    _assert_dry_then_real(cli, session, ["land"], lambda: _count(session, Track))


def test_sync_dry_run(cli, session) -> None:
    """`sync apple_xml -n` pulls no track the source playlist gained since `land`."""
    first, added = _track_data(name="Listed"), _track_data(name="Added later")
    do_import_tracks(session, {str(d["Track ID"]): d for d in (first, added)})
    do_import_playlists(session, [_playlist_data(track_ids=[first["Track ID"]])])
    unify_source_tracks(session)
    unify_source_playlists(session)
    # The owner adds a song to the playlist in Apple Music; the next export carries it.
    source_playlist = session.scalars(select(SourcePlaylist)).one()
    added_row = session.scalars(
        select(SourceTrack).where(SourceTrack.source_id == str(added["Track ID"]))
    ).one()
    session.add(SourcePlaylistTrack(playlist=source_playlist, track=added_row, position=2))
    _assert_dry_then_real(cli, session, ["sync", "apple_xml"], lambda: _count(session, PlaylistTrack))


def test_dedup_import_dry_run(cli, session, tmp_path) -> None:
    """`dedup-import FILE -n` stores no decision and writes no mirror."""
    src = tmp_path / "decisions.json"
    pair = {
        "a": ["one", "x", None, None],
        "b": ["two", "x", None, None],
        "kind": "same",
        "decided_at": "2026-01-01T00:00:00+00:00",
        "origin": "mcp",
    }
    src.write_text(json.dumps({"version": 2, "pairs": [pair], "canons": []}))

    _assert_dry_then_real(cli, session, ["dedup-import", str(src)], lambda: _count(session, PairDecision))


def test_renormalize_dry_run(cli, session) -> None:
    """`maint renormalize -n` leaves a stale `_norm` field stale."""
    track = make_track(session, title="Café")
    track.title_norm = "stale"
    session.flush()
    track_id = track.id  # the CLI closes the session, which detaches `track`

    def probe() -> str:
        return session.scalar(select(Track.title_norm).where(Track.id == track_id))

    _assert_dry_then_real(cli, session, ["maint", "renormalize"], probe)


def test_enrich_dry_run(cli, session, tmp_path, make_wav) -> None:
    """`maint enrich -n` stores no content hash."""
    tf = TrackFile(source_path=make_wav(tmp_path / "a.wav", seed=2, seconds=3), track=make_track(session))
    session.add(tf)
    session.flush()
    file_id = tf.id

    def probe() -> str | None:
        return session.scalar(select(TrackFile.content_hash).where(TrackFile.id == file_id))

    _assert_dry_then_real(cli, session, ["maint", "enrich"], probe)


def test_acoustid_dry_run(cli, session, monkeypatch) -> None:
    """`maint acoustid -n` keeps no recording id the lookup stored (the network call is stubbed)."""
    track_id = make_track(session, title="Looked up").id
    monkeypatch.setattr(settings, "acoustid_client_key", SecretStr("k"))

    def identify(s: Session, key: SecretStr, limit: int | None = None) -> IdentifyStats:
        s.get(Track, track_id).title = "renamed by the lookup"
        return IdentifyStats()

    monkeypatch.setattr("airdrome.terminal.maint.identify", identify)

    def probe() -> str:
        return session.scalar(select(Track.title).where(Track.id == track_id))

    _assert_dry_then_real(cli, session, ["maint", "acoustid"], probe)


def test_sync_navidrome_dry_run_leaves_navidrome_alone(cli, session, tmp_path, monkeypatch) -> None:
    """`sync navidrome -n` creates no playlist in Navidrome's own DB (outside the Airdrome session)."""
    db = tmp_path / "navidrome.db"
    engine = create_engine(f"sqlite:///{db}")
    NavidromeBase.metadata.create_all(engine)
    with Session(engine) as nvs:
        _seed_navidrome(nvs, username="owner")
        nvs.commit()
    monkeypatch.setattr(settings, "navidrome_db_dsn", f"sqlite:///{db}")
    monkeypatch.setattr(settings, "navidrome_user", "owner")
    monkeypatch.setattr(nv_models, "_engine", None)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        monkeypatch.setattr(settings, "navidrome_port", probe.getsockname()[1])

    track = make_track(session, title="Song")
    session.add(
        TrackFile(source_path=tmp_path / "song.mp3", library_path=LIB_PATH, is_main=True, track=track)
    )
    playlist = Playlist(name="Mix", platform=Source.APPLE_XML, source_id="mix")
    session.add(playlist)
    session.flush()
    session.add(PlaylistTrack(playlist_id=playlist.id, track_id=track.id, position=1))
    session.flush()

    def probe_nv() -> int:
        with Session(engine) as nvs:
            return nvs.scalar(select(func.count()).select_from(NVPlaylist))

    try:
        _assert_dry_then_real(cli, session, ["sync", "navidrome", "-y"], probe_nv)
    finally:
        if nv_models._engine is not None:
            nv_models._engine.dispose()
        engine.dispose()


def test_dedup_dry_run(cli, session) -> None:
    """`dedup -n` links no twin."""
    make_track(session, title="Twice", artist="Same", duration=200)
    make_track(session, title="Twice", artist="Same", album="Other", duration=200)
    session.flush()

    def probe() -> int:
        return session.scalar(select(func.count()).select_from(Track).where(Track.canon_id.is_not(None)))

    _assert_dry_then_real(cli, session, ["dedup"], probe)
