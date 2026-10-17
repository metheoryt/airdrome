from pathlib import Path

from sqlalchemy import select

from airdrome.cloud.apple.xml_library import do_import_tracks
from airdrome.cloud.sources import SourceTrack
from airdrome.enums import BindMethod
from airdrome.library.pathindex import PathTailIndex
from airdrome.library.unify import do_unify
from airdrome.models import Track, TrackFile
from tests.test_apple_ingest import _track_data


def _files(session, *paths: str) -> list[int]:
    """Persist unbound TrackFiles for ``paths`` and return their ids."""
    tfs = [TrackFile(source_path=Path(p)) for p in paths]
    session.add_all(tfs)
    session.flush()
    return [tf.id for tf in tfs]


def test_case_insensitive(session) -> None:
    """A differently-cased stored path still matches."""
    (fid,) = _files(session, "/Root/ARTIST/Album/01 Song.MP3")
    assert PathTailIndex.build(session).lookup("Artist/Album/01 Song.mp3") == [fid]


def test_underscore_literal(session) -> None:
    """'_' is a plain character, not a single-char wildcard."""
    real, other = _files(session, "/r/AC_DC/x.mp3", "/r/ACxDC/x.mp3")
    index = PathTailIndex.build(session)
    assert index.lookup("AC_DC/x.mp3") == [real]
    assert index.lookup("ACxDC/x.mp3") == [other]


def test_percent_literal_and_boundary(session) -> None:
    """'%' is literal and a match must start on a path-component boundary."""
    _files(session, "/r/DJ Artist/Album/x.mp3", "/r/100%/x.mp3")
    index = PathTailIndex.build(session)
    assert index.lookup("Artist/Album/x.mp3") == []
    assert index.lookup("%/x.mp3") == []


def test_tail_hits_two_roots(session) -> None:
    """The same tail under two roots returns both ids."""
    a, b = _files(session, "/rootA/Artist/x.mp3", "/rootB/Artist/x.mp3")
    assert sorted(PathTailIndex.build(session).lookup("Artist/x.mp3")) == [a, b]


def test_claimed_file_not_returned(session) -> None:
    """A claimed file drops out of later lookups."""
    a, b = _files(session, "/rootA/Artist/x.mp3", "/rootB/Artist/x.mp3")
    index = PathTailIndex.build(session)
    index.claim(a)
    assert index.lookup("Artist/x.mp3") == [b]


def test_bound_files_not_indexed(session) -> None:
    """Files already on a track are not in the index."""
    track = Track(title="T", artist="A", album="B", album_artist="A")
    session.add(track)
    session.flush()
    session.add(TrackFile(source_path=Path("/r/Artist/x.mp3"), track_id=track.id))
    session.flush()
    assert PathTailIndex.build(session).lookup("Artist/x.mp3") == []


def test_path_beats_matching_tags(session) -> None:
    """A file tagged like Track X but pathed like source track Y ends on Y with APPLE_PATH."""
    x = Track(title="Tagged Song", artist="Tag Artist", album="Tag Album", album_artist="Tag Artist")
    session.add(x)
    data = _track_data(name="Path Song", artist="Path Artist", album="Path Album", album_artist="Path Artist")
    do_import_tracks(session, {str(data["Track ID"]): data})
    st = session.scalars(select(SourceTrack).where(SourceTrack.source_id == str(data["Track ID"]))).one()
    rel = st.possible_locations(max_suffix=2)[0]
    tf = TrackFile(
        source_path=Path("/root") / rel,
        title="Tagged Song",
        artist="Tag Artist",
        album="Tag Album",
        album_artist="Tag Artist",
    )
    session.add(tf)
    session.flush()

    do_unify(session)

    session.refresh(tf)
    y = session.scalars(select(Track).where(Track.title == "Path Song")).one()
    assert tf.track_id == y.id != x.id
    assert tf.bind_method == BindMethod.APPLE_PATH
    assert tf.bind_confidence == 0.9
