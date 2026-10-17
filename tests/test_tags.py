import hashlib
from pathlib import Path

import pytest
from mutagen.id3 import UFID
from mutagen.mp4 import MP4FreeForm

from airdrome import models
from airdrome.enums import BindMethod
from airdrome.library.tags import AUDIO_EXTENSIONS, FileTags, extract_tags, file_md5
from airdrome.models import TrackFile


def test_mp3_and_m4a_ids_equal() -> None:
    """The same recording id reads identically from ID3 UFID and MP4 freeform (bytes decoded)."""
    mp3 = extract_tags({"UFID:http://musicbrainz.org": UFID(owner="http://musicbrainz.org", data=b"abc-1")})
    m4a = extract_tags({"----:com.apple.iTunes:MusicBrainz Track Id": [MP4FreeForm(b"abc-1")]})
    assert mp3["recording_mbid"] == m4a["recording_mbid"] == "abc-1"


def test_acoustid_keys_decode() -> None:
    """AcoustID ids read from MP4 freeform and Vorbis keys."""
    m4a = extract_tags({"----:com.apple.iTunes:Acoustid Id": [MP4FreeForm(b"aid")]})
    ogg = extract_tags({"acoustid_id": ["aid"]})
    assert m4a["acoustid_id"] == ogg["acoustid_id"] == "aid"


def test_vorbis_keys() -> None:
    """Vorbis comment keys map onto the common fields."""
    t = extract_tags({"title": ["T"], "albumartist": ["AA"], "musicbrainz_trackid": ["m"]})
    assert (t["title"], t["album_artist"], t["recording_mbid"]) == ("T", "AA", "m")


def test_asf_keys() -> None:
    """ASF attribute names map onto the common fields."""
    t = extract_tags({"Title": ["T"], "Author": ["A"], "WM/AlbumTitle": ["Al"], "WM/Year": ["2001"]})
    assert (t["title"], t["artist"], t["album"], t["date"]) == ("T", "A", "Al", "2001")


def test_missing_ids_none() -> None:
    """No id tags yields None, not an empty string."""
    t = extract_tags({"title": ["T"]})
    assert t["recording_mbid"] is None
    assert t["acoustid_id"] is None


def test_file_md5(tmp_path: Path) -> None:
    """The digest equals hashlib's over the same bytes, across chunk boundaries."""
    data = b"x" * (1024 * 1024 + 5)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert file_md5(p) == hashlib.md5(data).hexdigest()


def test_scanner_accepts_upper_and_new_suffixes() -> None:
    """Containers beyond mp3/m4a/flac are accepted, compared lower-cased."""
    for name in ("a.OGG", "b.opus", "c.wav"):
        assert Path(name).suffix.lower() in AUDIO_EXTENSIONS


def _fake_tags(mbid: str | None) -> FileTags:
    """Build a FileTags carrying only a recording id."""
    return FileTags(
        title="T", artist="A", album="Al", album_artist=None, date=None,
        recording_mbid=mbid, acoustid_id=None, duration=1.0, bitrate=320000,
    )  # fmt: skip


def test_enrich_tag_id_sets_mbid_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag id is stored with source MBID, and the md5 is filled."""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"abc")
    monkeypatch.setattr(models, "read_tags", lambda _p: _fake_tags("m-1"))
    tf = TrackFile(source_path=f)
    tf.enrich()
    assert (tf.recording_mbid, tf.mbid_source) == ("m-1", BindMethod.MBID)
    assert tf.content_hash == hashlib.md5(b"abc").hexdigest()


def test_enrich_keeps_acoustid_lookup_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a tag id, a looked-up id survives a re-read but a stale tag id is cleared."""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"abc")
    monkeypatch.setattr(models, "read_tags", lambda _p: _fake_tags(None))
    looked_up = TrackFile(source_path=f, recording_mbid="x", mbid_source=BindMethod.ACOUSTID)
    looked_up.enrich()
    assert (looked_up.recording_mbid, looked_up.mbid_source) == ("x", BindMethod.ACOUSTID)
    stale = TrackFile(source_path=f, recording_mbid="y", mbid_source=BindMethod.MBID)
    stale.enrich()
    assert (stale.recording_mbid, stale.mbid_source) == (None, None)
