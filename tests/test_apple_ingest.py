from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from airdrome.cloud.apple.xml_library import do_import_playlists, do_import_tracks
from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.enums import Source
from airdrome.library.unify import do_unify, unify_source_playlists, unify_source_tracks
from airdrome.models import Backend, Playlist, PlaylistLink, PlaylistTrack, Track, TrackFile


def _xml_track(session, source_id):
    return session.scalars(
        select(SourceTrack).where(
            SourceTrack.provider == Source.APPLE_XML, SourceTrack.source_id == str(source_id)
        )
    )


# ── helpers ──────────────────────────────────────────────────────────────────

_DATE = datetime(2020, 1, 1, tzinfo=UTC)
_COUNTER = iter(range(1, 10_000))


def _track_data(
    name: str = "Test Track",
    artist: str = "Test Artist",
    album: str = "Test Album",
    album_artist: str = "Test Artist",
    apple_music: bool = True,
) -> dict:
    uid = next(_COUNTER)
    return {
        "Track ID": uid,
        "Name": name,
        "Artist": artist,
        "Album": album,
        "Album Artist": album_artist,
        "Apple Music": apple_music,
        "Date Added": _DATE,
        "Date Modified": _DATE,
        "Size": 1_000_000,
        "Track Type": "Remote",
        "Persistent ID": f"PERSIST{uid:05d}",
    }


def _playlist_data(
    name: str = "Test Playlist",
    track_ids: list[int] = (),
) -> dict:
    uid = next(_COUNTER)
    return {
        "Playlist ID": uid,
        "Name": name,
        "Playlist Persistent ID": f"PL{uid:05d}",
        "Description": "",
        "All Items": True,
        "Playlist Items": [{"Track ID": tid} for tid in track_ids],
    }


# ── track import tests ────────────────────────────────────────────────────────


def test_import_tracks_creates_apple_track(session):
    data = _track_data(name="My Song")
    track_id = data["Track ID"]

    do_import_tracks(session, {str(track_id): data})

    apple_track = _xml_track(session, track_id).one()
    assert apple_track.title == "My Song"


def test_import_tracks_idempotent(session):
    data = _track_data()
    track_id = data["Track ID"]
    tracks = {str(track_id): data}

    first = do_import_tracks(session, tracks)
    second = do_import_tracks(session, tracks)

    assert first == 1
    assert second == 0
    assert len(_xml_track(session, track_id).all()) == 1


def test_import_tracks_no_track_id_before_unify(session):
    data = _track_data()
    do_import_tracks(session, {str(data["Track ID"]): data})

    apple_track = _xml_track(session, data["Track ID"]).one()
    assert apple_track.track_id is None


# ── unify tests ───────────────────────────────────────────────────────────────


def test_unify_creates_track(session):
    data = _track_data(name="My Song", artist="My Artist")
    do_import_tracks(session, {str(data["Track ID"]): data})

    unify_source_tracks(session)

    track = session.scalars(select(Track).where(Track.title == "My Song")).one()
    assert track.artist == "My Artist"


def test_unify_links_apple_track(session):
    data = _track_data(name="My Song")
    do_import_tracks(session, {str(data["Track ID"]): data})
    unify_source_tracks(session)

    apple_track = _xml_track(session, data["Track ID"]).one()
    assert apple_track.track_id is not None


def test_unify_reuses_existing_track(session):
    """Two Apple tracks with same title/artist should link to the same canonical Track."""
    d1 = _track_data(name="Same Song", artist="Same Artist")
    d2 = _track_data(name="Same Song", artist="Same Artist")
    do_import_tracks(session, {str(d1["Track ID"]): d1, str(d2["Track ID"]): d2})

    unify_source_tracks(session)

    tracks_in_db = session.scalars(select(Track).where(Track.title == "Same Song")).all()
    assert len(tracks_in_db) == 1


def test_unify_binds_multiple_files_for_one_path(session):
    """One rel_path matching files under two roots binds both (no MultipleResultsFound, no dup)."""
    data = _track_data(name="Dup Song", artist="Dup Artist")
    do_import_tracks(session, {str(data["Track ID"]): data})

    st = _xml_track(session, data["Track ID"]).one()
    rel = st.possible_locations(max_suffix=2)[0]

    # Same relative tail under two distinct roots — both contain `rel` as a substring.
    f1 = TrackFile(source_path=Path("/rootA") / rel)
    f2 = TrackFile(source_path=Path("/rootB") / rel)
    session.add_all([f1, f2])
    session.flush()

    _, _, files_bound = unify_source_tracks(session)

    assert files_bound == 2
    track = session.scalars(select(Track).where(Track.title == "Dup Song")).one()
    assert {tf.id for tf in track.files} == {f1.id, f2.id}


def test_unify_binds_file_with_differing_case(session):
    """A file whose on-disk path differs only in case still binds (icontains, not contains)."""
    data = _track_data(name="Case Song", artist="Case Artist")
    do_import_tracks(session, {str(data["Track ID"]): data})

    st = _xml_track(session, data["Track ID"]).one()
    rel = st.possible_locations(max_suffix=2)[0]

    # Store the file with an upper-cased path; a case-sensitive LIKE would miss it.
    tf = TrackFile(source_path=Path("/root") / rel.upper())
    session.add(tf)
    session.flush()

    _, _, files_bound = unify_source_tracks(session)

    assert files_bound == 1
    track = session.scalars(select(Track).where(Track.title == "Case Song")).one()
    assert {f.id for f in track.files} == {tf.id}


def test_unify_no_not_found_when_sibling_already_bound(session, monkeypatch):
    """A local-file-expecting XML track must not warn 'not found' when a sibling already bound the file.

    The same physical file commonly has both an Apple XML and an Apple MS source row resolving to one
    canonical track; whichever is processed first binds the file. The other must stay quiet, not warn.
    """
    import airdrome.library.unify as unify_mod

    # The canonical track + file a sibling source row would already have produced this run.
    track = Track(title="Sib Song", artist="Sib Artist", album="Sib Album", album_artist="Sib Artist")
    session.add(track)
    session.flush()
    session.add(TrackFile(source_path=Path("/root/Sib Artist/Sib Album/01 Sib Song.mp3"), track_id=track.id))
    session.flush()

    # XML row for the same metadata that expects a local file (apple_music=False).
    data = _track_data(
        name="Sib Song", artist="Sib Artist", album="Sib Album", album_artist="Sib Artist", apple_music=False
    )
    do_import_tracks(session, {str(data["Track ID"]): data})

    warnings: list[str] = []
    monkeypatch.setattr(unify_mod.console, "print", lambda *a, **k: warnings.append(str(a[0]) if a else ""))
    unify_source_tracks(session)

    assert not any("not found" in w for w in warnings)
    st = _xml_track(session, data["Track ID"]).one()
    assert st.track_id == track.id  # linked to the same canonical track the sibling's file is on


def test_unify_idempotent(session):
    data = _track_data()
    do_import_tracks(session, {str(data["Track ID"]): data})

    first_created, *_ = unify_source_tracks(session)
    session.flush()
    second_created, *_ = unify_source_tracks(session)

    assert first_created == 1
    assert second_created == 0


# ── playlist import tests ─────────────────────────────────────────────────────


def test_import_playlists_creates_playlist(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})

    pl = _playlist_data(name="My Playlist", track_ids=[track_data["Track ID"]])
    created = do_import_playlists(session, [pl])

    assert created == 1
    pl_db = session.scalars(
        select(SourcePlaylist).where(SourcePlaylist.source_id == pl["Playlist Persistent ID"])
    ).one()
    assert pl_db.name == "My Playlist"


def test_import_playlists_skips_smart_playlists(session):
    pl = _playlist_data()
    pl["Smart Info"] = b"bplist00"

    created = do_import_playlists(session, [pl])

    assert created == 0
    assert len(session.scalars(select(SourcePlaylist)).all()) == 0


def test_import_playlists_idempotent(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})

    pl = _playlist_data(track_ids=[track_data["Track ID"]])
    first = do_import_playlists(session, [pl])
    second = do_import_playlists(session, [pl])

    assert first == 1
    assert second == 0


# ── playlist unify tests ──────────────────────────────────────────────────────


def test_unify_playlists_creates_canonical_playlist(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})
    unify_source_tracks(session)

    pl = _playlist_data(name="Unified Playlist", track_ids=[track_data["Track ID"]])
    do_import_playlists(session, [pl])

    pl_created, tr_linked = unify_source_playlists(session)

    assert pl_created == 1
    assert tr_linked == 1
    playlist = session.scalars(select(Playlist).where(Playlist.name == "Unified Playlist")).one()
    assert playlist is not None
    pt = session.scalars(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id)).all()
    assert len(pt) == 1


def test_unify_playlists_idempotent(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})
    unify_source_tracks(session)

    pl = _playlist_data(track_ids=[track_data["Track ID"]])
    do_import_playlists(session, [pl])

    first_pl, _first_tr = unify_source_playlists(session)
    session.flush()
    second_pl, second_tr = unify_source_playlists(session)

    assert first_pl == 1
    assert second_pl == 0
    assert second_tr == 0


def _canonical_track_id(session, name: str) -> int:
    return session.scalars(select(Track).where(Track.title == name)).one().id


def test_unify_playlists_default_keeps_same_name_separate(session):
    a = _track_data(name="A")
    b = _track_data(name="B")
    do_import_tracks(session, {str(a["Track ID"]): a, str(b["Track ID"]): b})
    unify_source_tracks(session)

    # Two distinct source playlists (distinct persistent IDs) sharing a name.
    do_import_playlists(
        session,
        [
            _playlist_data(name="Dup", track_ids=[a["Track ID"]]),
            _playlist_data(name="Dup", track_ids=[b["Track ID"]]),
        ],
    )

    pl_created, _ = unify_source_playlists(session)  # default: no name merge

    assert pl_created == 2
    canonicals = session.scalars(select(Playlist).where(Playlist.name == "Dup")).all()
    assert len(canonicals) == 2
    # Each canonical carries only its own source's track.
    members = {frozenset(pt.track_id for pt in pl.tracks) for pl in canonicals}
    assert members == {
        frozenset({_canonical_track_id(session, "A")}),
        frozenset({_canonical_track_id(session, "B")}),
    }


def test_unify_playlists_merge_by_name_collapses(session):
    a = _track_data(name="A")
    b = _track_data(name="B")
    do_import_tracks(session, {str(a["Track ID"]): a, str(b["Track ID"]): b})
    unify_source_tracks(session)

    # Overlapping membership: the shared track B must not be linked twice.
    do_import_playlists(
        session,
        [
            _playlist_data(name="Dup", track_ids=[a["Track ID"], b["Track ID"]]),
            _playlist_data(name="Dup", track_ids=[b["Track ID"]]),
        ],
    )

    pl_created, tracks_linked = unify_source_playlists(session, merge_by_name=True)

    assert pl_created == 1
    assert tracks_linked == 2  # A and B once each, no duplicate B
    canonical = session.scalars(select(Playlist).where(Playlist.name == "Dup")).one()
    assert {pt.track_id for pt in canonical.tracks} == {
        _canonical_track_id(session, "A"),
        _canonical_track_id(session, "B"),
    }


def test_do_unify_rebuild_playlists_drops_and_recreates(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})
    do_import_playlists(session, [_playlist_data(name="Real", track_ids=[track_data["Track ID"]])])

    # First pass builds the source-backed canonical, then we attach a backend link and a
    # stale canonical that no source claims — both must not survive a rebuild.
    do_unify(session)
    real = session.scalars(select(Playlist).where(Playlist.name == "Real")).one()
    session.add(
        PlaylistLink(
            playlist_id=real.id,
            backend=Backend.NAVIDROME,
            external_id="nd-1",
            synced_track_ids=[],
            synced_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    stale = Playlist(name="Stale", platform=Source.NAVIDROME, source_id="gone")
    session.add(stale)
    session.flush()

    do_unify(session, rebuild_playlists=True)

    # The source-backed playlist is rebuilt; the stale one and the link are gone.
    names = set(session.scalars(select(Playlist.name)).all())
    assert names == {"Real"}
    assert session.scalars(select(PlaylistLink)).all() == []
    rebuilt = session.scalars(select(Playlist).where(Playlist.name == "Real")).one()
    assert {pt.track_id for pt in rebuilt.tracks} == {_canonical_track_id(session, "Test Track")}
