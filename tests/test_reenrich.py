from pathlib import Path

import pytest

from airdrome import models
from airdrome.enums import BindMethod
from airdrome.library import fingerprint
from airdrome.library.enrich import fingerprint_files, reenrich
from airdrome.library.tags import FileTags
from airdrome.models import TrackFile
from tests.factories import make_track


@pytest.fixture()
def wav_file(session, tmp_path: Path, make_wav) -> TrackFile:
    """A bound TrackFile pointing at a short real wav (no tags, no ids)."""
    track = make_track(session, title="bound")
    path = make_wav(tmp_path / "a.wav", seed=1, seconds=3)
    tf = TrackFile(source_path=path, track=track)
    session.add(tf)
    session.flush()
    return tf


@pytest.fixture()
def compute_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap `fingerprint.compute`, recording every path it is called with."""
    calls: list[Path] = []
    real = fingerprint.compute

    def counting(path: Path) -> fingerprint.Fingerprint:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(fingerprint, "compute", counting)
    return calls


def test_fills_hash_and_fp(session, wav_file: TrackFile) -> None:
    """A fresh file gets its md5, fingerprint and duration filled in."""
    stats = reenrich(session)
    assert (stats.seen, stats.updated, stats.missing, stats.fp_failed, stats.with_fp) == (1, 1, 0, 0, 1)
    assert wav_file.content_hash
    assert wav_file.fingerprint
    assert wav_file.fp_duration == pytest.approx(3, abs=0.5)


def test_skips_unchanged(session, wav_file: TrackFile, compute_calls: list[Path]) -> None:
    """A second run with unchanged bytes re-reads and re-fingerprints nothing."""
    reenrich(session)
    compute_calls.clear()
    stats = reenrich(session)
    assert (stats.updated, stats.with_fp) == (0, 1)
    assert compute_calls == []
    reenrich(session, force=True)
    assert len(compute_calls) == 1


def test_changed_file_refingerprinted(session, wav_file: TrackFile, make_wav) -> None:
    """New bytes under the same path change the hash and the fingerprint."""
    reenrich(session)
    old_hash, old_fp = wav_file.content_hash, wav_file.fingerprint
    make_wav(wav_file.source_path, seed=2, seconds=3)
    assert reenrich(session).updated == 1
    assert wav_file.content_hash != old_hash
    assert wav_file.fingerprint != old_fp


def test_keeps_binding(session, wav_file: TrackFile) -> None:
    """Re-enriching never rebinds a file to another track."""
    track_id = wav_file.track_id
    reenrich(session, force=True)
    assert wav_file.track_id == track_id


def test_missing_file_counted(session, wav_file: TrackFile) -> None:
    """A vanished file is counted, not fatal."""
    wav_file.source_path.unlink()
    stats = reenrich(session)
    assert (stats.seen, stats.missing, stats.updated) == (1, 1, 0)


def test_unreadable_file_counted(session, tmp_path: Path) -> None:
    """A corrupt audio file (mutagen error) is counted as unreadable, not fatal."""
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all" * 100)
    session.add(TrackFile(source_path=bad))
    session.flush()
    stats = reenrich(session)
    assert (stats.seen, stats.missing, stats.updated) == (1, 1, 0)


def test_fingerprint_files_counts_failures(session, wav_file: TrackFile, tmp_path: Path) -> None:
    """`fingerprint_files` returns the number fingerprinted and survives a failing file."""
    broken = TrackFile(source_path=tmp_path / "gone.wav")
    session.add(broken)
    session.flush()
    assert fingerprint_files(session, [wav_file, broken], workers=2) == 1
    assert wav_file.fingerprint
    assert broken.fingerprint is None


def test_with_mbid_coverage(session, wav_file: TrackFile, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file whose tags carry a recording id is counted in `with_mbid` and bound as an MBID source."""
    monkeypatch.setattr(models, "read_tags", lambda _p: FileTags(
        title="T", artist="A", album="Al", album_artist=None, date=None,
        recording_mbid="m-1", acoustid_id=None, duration=3.0, bitrate=0,
    ))  # fmt: skip
    stats = reenrich(session)
    assert (stats.with_mbid, wav_file.recording_mbid, wav_file.mbid_source) == (1, "m-1", BindMethod.MBID)


def test_force_keeps_fingerprint_on_transient_failure(
    session, wav_file: TrackFile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing fpcalc under --force on unchanged bytes leaves the stored fingerprint intact."""
    reenrich(session)
    assert wav_file.fingerprint is not None
    good = list(wav_file.fingerprint)

    def boom(_p: Path) -> fingerprint.Fingerprint:
        raise fingerprint.FingerprintError("transient")

    monkeypatch.setattr(fingerprint, "compute", boom)
    stats = reenrich(session, force=True)
    assert (stats.fp_failed, stats.with_fp) == (1, 1)
    assert wav_file.fingerprint == good
