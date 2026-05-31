"""Detection tests for the unified `import` source registry.

Each fixture builds a minimal artifact in the shape a real export takes, and asserts
exactly one importer claims it (detection is content-sniffed, so formats sharing an
extension or a zip/dir shape must not collide).
"""

import io
import json
import zipfile

import pytest

from airdrome.ingest import detect
from airdrome.ingest.sources import (
    AppleMsImporter,
    AppleXmlImporter,
    LastFmImporter,
    ListenBrainzImporter,
    MusicFolderImporter,
    SpotifyImporter,
)


def _apple_xml(tmp_path):
    p = tmp_path / "Library.xml"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>Tracks</key><dict/></dict></plist>',
        encoding="utf-8",
    )
    return p


def _spotify(tmp_path):
    d = tmp_path / "spotify"
    d.mkdir()
    record = {"master_metadata_track_name": "Song", "ts": "2024-01-01T00:00:00Z", "ms_played": 99999}
    (d / "StreamingHistory0.json").write_text(json.dumps([record]), encoding="utf-8")
    return d


def _listenbrainz_zip(tmp_path):
    p = tmp_path / "lb_export.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("listens/2024/1.jsonl", '{"listened_at": 1, "track_metadata": {}}\n')
    return p


def _lastfm(tmp_path):
    p = tmp_path / "scrobbles.csv"
    p.write_text("Artist,Album,Track,01 Jan 2024 12:00\n", encoding="utf-8")
    return p


def _apple_ms_zip(tmp_path):
    # Mirror the real export: the signature files live inside a nested
    # Apple_Media_Services.zip, not at the top level.
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as iz:
        iz.writestr("Apple_Media_Services/Apple Music Activity/Apple Music Library Tracks.json.zip", b"stub")
        iz.writestr("Apple_Media_Services/Apple Music Activity/Apple Music Play Activity.csv", b"stub")
    p = tmp_path / "Apple Media Services information Part 1 of 2.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Part 1 of 2/Update and Redownload History/x.csv", b"stub")
        z.writestr("Part 1 of 2/Apple_Media_Services.zip", inner.getvalue())
    return p


def _music_folder(tmp_path):
    d = tmp_path / "music"
    (d / "Artist" / "Album").mkdir(parents=True)
    (d / "Artist" / "Album" / "track.mp3").write_bytes(b"\x00")
    return d


_CASES = [
    (_apple_xml, AppleXmlImporter),
    (_spotify, SpotifyImporter),
    (_listenbrainz_zip, ListenBrainzImporter),
    (_lastfm, LastFmImporter),
    (_apple_ms_zip, AppleMsImporter),
    (_music_folder, MusicFolderImporter),
]


@pytest.mark.parametrize("build, expected", _CASES, ids=[c[1].__name__ for c in _CASES])
def test_detects_exactly_one(build, expected, tmp_path):
    path = build(tmp_path)
    matches = detect(path)
    assert matches == [expected], f"{path.name} matched {[m.name for m in matches]}"


def test_unrecognized_returns_empty(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("just some text", encoding="utf-8")
    assert detect(p) == []


def test_empty_dir_unrecognized(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert detect(d) == []
