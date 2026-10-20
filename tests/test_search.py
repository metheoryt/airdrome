"""Tests for `search`: whole-word matching, track groups with files, and the walk of the music folders."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from airdrome.cloud.sources import SourceTrack
from airdrome.conf import settings
from airdrome.enums import Source
from airdrome.library.search import matches, search, words
from airdrome.models import TrackFile, TrackPlay

from factories import make_track


@pytest.mark.parametrize(
    ("query", "target", "hit"),
    [
        ("a-ha", "Baha Men/Who Let the Dogs Out", False),  # `aha` is inside `baha`, but not a word of it
        ("a-ha", "a-ha/25", True),
        ("The Singles: 1984-2004", "a-ha/The Singles_ 1984-2004", True),  # Picard writes `:` as `_`
        ("take on me a-ha", "a-ha/Take On Me", True),  # any order
        ("take on m", "Take On Me", True),  # the last word may be cut short
        ("tak on me", "Take On Me", False),  # only the last one
        ("Beyonce", "Beyoncé - Halo", True),
    ],
)
def test_matches(query, target, hit):
    assert matches(words(query), words(target)) is hit


def test_empty_query_is_refused(session):
    with pytest.raises(ValueError, match="no words"):
        search(session, " - ")


@pytest.fixture()
def music(tmp_path, monkeypatch) -> Path:
    """A music folder named `me` (so a root's own name never matches) and an empty library beside it."""
    root = tmp_path / "me"
    root.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(settings, "search_dirs", [root])
    monkeypatch.setattr(settings, "library_dir", lib)
    return root


def _touch(root: Path, rel: str) -> Path:
    """Create an empty file at `root/rel`, parents included."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def test_tracks_are_canon_groups_with_files_most_played_first(session, music):
    canon = make_track(session, "Take On Me", "a-ha", "The Singles: 1984-2004")
    twin = make_track(
        session, "Take On Me (2015 Remastered)", "a-ha", "Hunting High and Low", canon_id=canon.id
    )
    cover = make_track(session, "Take On Me", "Reel Big Fish", "Greatest Hit And More")
    make_track(session, "Take Five", "Dave Brubeck")
    session.add(SourceTrack(provider=Source.APPLE_XML, source_id="x", track_id=twin.id, title=twin.title))
    session.add(TrackFile(source_path=music / "rbf.mp3", track_id=cover.id, is_main=True, bitrate=192))
    for i in range(2):
        session.add(
            TrackPlay(
                track_id=twin.id, played_at=datetime(2022, 1, 1 + i, tzinfo=UTC), platform=Source.LASTFM
            )
        )
    session.flush()

    tracks = search(session, "take on me")["tracks"]

    assert tracks["total"] == 2
    first, second = tracks["rows"]
    assert (first["track_id"], first["members"], first["matched_member_ids"]) == (
        canon.id,
        2,
        [canon.id, twin.id],
    )
    assert (first["plays"], first["in_library"], first["has_file"]) == (2, True, False)
    assert second["track_id"] == cover.id
    assert second["files"] == [
        {"track_id": cover.id, "path": str(music / "rbf.mp3"), "is_main": True, "bitrate": 192}
    ]


def test_folder_hit_folds_its_subtree_and_names_folders_without_audio(session, music):
    t = make_track(session, "Velvet", "a-ha")
    known = _touch(music, "PicardedMusic/a-ha/25/2-09 Velvet.mp3")
    session.add(TrackFile(source_path=known, track_id=t.id))
    session.flush()
    _touch(music, "PicardedMusic/a-ha/25/2-10 Forever Not Yours.mp3")
    _touch(music, "PicardedMusic/a-ha/The Singles_ 1984-2004/Folder.jpg")
    _touch(music, "PicardedMusic/a-ha/The Singles_ 1984-2004/Scans/back.jpg")
    _touch(music, "OldMusic/Baha Men - Who Let the Dogs Out.mp3")

    disk = search(session, "a-ha")["disk"]

    assert disk["roots"] == [str(music), str(settings.library_dir)]
    [hit] = disk["rows"]  # one row for the artist folder: its albums are folded in, Baha Men is not a match
    assert hit["kind"] == "folder"
    assert hit["path"] == str(music / "PicardedMusic/a-ha")
    assert (hit["audio_files"], hit["imported"]) == (2, 1)
    assert hit["folders_without_audio"] == ["The Singles_ 1984-2004"]  # topmost only, not its Scans/


def test_files_match_on_their_folders_and_name(session, music):
    t = make_track(session, "Velvet", "a-ha")
    known = _touch(music, "a-ha/25/2-09 Velvet.mp3")
    session.add(TrackFile(source_path=known, track_id=t.id))
    session.flush()
    loose = _touch(music, "OldMusic/a-ha - Velvet.mp3")
    _touch(music, "OldMusic/velvet.txt")  # not audio

    rows = search(session, "velvet a-ha")["disk"]["rows"]

    assert {(r["kind"], Path(r["path"]).name, r["track_id"]) for r in rows} == {
        ("file", known.name, t.id),
        ("file", loose.name, None),  # never imported
    }


def test_root_name_is_not_matched(session, music):
    _touch(music, "x/Song.mp3")
    assert search(session, "me")["disk"]["total"] == 0


def test_library_copies_count_as_imported(session, music):
    t = make_track(session, "Song", "Band")
    session.add(
        TrackFile(
            source_path=Path("/gone/song.mp3"),
            library_path=Path("Library/Music/Band/Song.mp3"),
            track_id=t.id,
        )
    )
    session.flush()
    _touch(settings.library_dir, "Library/Music/Band/Song.mp3")

    [hit] = search(session, "band")["disk"]["rows"]
    assert (hit["path"].endswith("lib/Library/Music/Band"), hit["imported"]) == (True, 1)


def test_sections_are_capped_with_totals(session, music):
    for i in range(3):
        make_track(session, f"Song {i}", "Band")
        _touch(music, f"Band {i}/a.mp3")
    res = search(session, "band", limit=2)
    assert (res["tracks"]["total"], len(res["tracks"]["rows"])) == (3, 2)
    assert (res["disk"]["total"], len(res["disk"]["rows"])) == (3, 2)
