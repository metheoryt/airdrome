import pytest
from sqlalchemy.exc import IntegrityError

from airdrome.enums import BindMethod
from airdrome.models import ScanCache, TrackFile


def test_new_columns_round_trip(session):
    """Every identity-ladder column on TrackFile survives a flush/expire/reload."""
    f = TrackFile(
        source_path="/music/a.flac",
        recording_mbid="11111111-2222-3333-4444-555555555555",
        mbid_source=BindMethod.ACOUSTID,
        acoustid_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        content_hash="d41d8cd98f00b204e9800998ecf8427e",
        fingerprint=[1, -2, 3],
        fp_duration=215.5,
        bind_method=BindMethod.APPLE_PATH,
        bind_confidence=0.75,
    )
    session.add(f)
    session.flush()
    session.expire(f)

    assert f.recording_mbid == "11111111-2222-3333-4444-555555555555"
    assert f.mbid_source is BindMethod.ACOUSTID
    assert f.acoustid_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert f.content_hash == "d41d8cd98f00b204e9800998ecf8427e"
    assert f.fingerprint == [1, -2, 3]
    assert f.fp_duration == 215.5
    assert f.bind_method is BindMethod.APPLE_PATH
    assert f.bind_confidence == 0.75


def test_scancache_path_unique(session):
    """Two scan-cache rows may not share a path."""
    session.add(ScanCache(path="/music/a.flac", size=1, mtime=1.0))
    session.flush()
    session.add(ScanCache(path="/music/a.flac", size=2, mtime=2.0))
    with pytest.raises(IntegrityError):
        session.flush()
