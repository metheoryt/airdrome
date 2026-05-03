from datetime import UTC, datetime

from sqlmodel import select

from airdrome.cloud.apple.models import ApplePlaylist, AppleTrack
from airdrome.cloud.apple.unify import unify_apple_playlists, unify_apple_tracks
from airdrome.cloud.apple.xml_library import do_import_playlists, do_import_tracks
from airdrome.models import Playlist, PlaylistTrack, Track


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

    apple_track = session.exec(select(AppleTrack).where(AppleTrack.apple_track_id == track_id)).one()
    assert apple_track.name == "My Song"


def test_import_tracks_idempotent(session):
    data = _track_data()
    track_id = data["Track ID"]
    tracks = {str(track_id): data}

    first = do_import_tracks(session, tracks)
    second = do_import_tracks(session, tracks)

    assert first == 1
    assert second == 0
    assert len(session.exec(select(AppleTrack).where(AppleTrack.apple_track_id == track_id)).all()) == 1


def test_import_tracks_no_track_id_before_unify(session):
    data = _track_data()
    do_import_tracks(session, {str(data["Track ID"]): data})

    apple_track = session.exec(select(AppleTrack).where(AppleTrack.apple_track_id == data["Track ID"])).one()
    assert apple_track.track_id is None


# ── unify tests ───────────────────────────────────────────────────────────────


def test_unify_creates_track(session):
    data = _track_data(name="My Song", artist="My Artist")
    do_import_tracks(session, {str(data["Track ID"]): data})

    unify_apple_tracks(session)

    track = session.exec(select(Track).where(Track.title == "My Song")).one()
    assert track.artist == "My Artist"


def test_unify_links_apple_track(session):
    data = _track_data(name="My Song")
    do_import_tracks(session, {str(data["Track ID"]): data})
    unify_apple_tracks(session)

    apple_track = session.exec(select(AppleTrack).where(AppleTrack.apple_track_id == data["Track ID"])).one()
    assert apple_track.track_id is not None


def test_unify_reuses_existing_track(session):
    """Two Apple tracks with same title/artist should link to the same canonical Track."""
    d1 = _track_data(name="Same Song", artist="Same Artist")
    d2 = _track_data(name="Same Song", artist="Same Artist")
    do_import_tracks(session, {str(d1["Track ID"]): d1, str(d2["Track ID"]): d2})

    unify_apple_tracks(session)

    tracks_in_db = session.exec(select(Track).where(Track.title == "Same Song")).all()
    assert len(tracks_in_db) == 1


def test_unify_idempotent(session):
    data = _track_data()
    do_import_tracks(session, {str(data["Track ID"]): data})

    first_created, _ = unify_apple_tracks(session)
    session.flush()
    second_created, _ = unify_apple_tracks(session)

    assert first_created == 1
    assert second_created == 0


# ── playlist import tests ─────────────────────────────────────────────────────


def test_import_playlists_creates_playlist(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})

    pl = _playlist_data(name="My Playlist", track_ids=[track_data["Track ID"]])
    created = do_import_playlists(session, [pl])

    assert created == 1
    pl_db = session.exec(select(ApplePlaylist).where(ApplePlaylist.playlist_id == pl["Playlist ID"])).one()
    assert pl_db.name == "My Playlist"


def test_import_playlists_skips_smart_playlists(session):
    pl = _playlist_data()
    pl["Smart Info"] = b"bplist00"

    created = do_import_playlists(session, [pl])

    assert created == 0
    assert len(session.exec(select(ApplePlaylist)).all()) == 0


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
    unify_apple_tracks(session)

    pl = _playlist_data(name="Unified Playlist", track_ids=[track_data["Track ID"]])
    do_import_playlists(session, [pl])

    pl_created, tr_linked = unify_apple_playlists(session)

    assert pl_created == 1
    assert tr_linked == 1
    playlist = session.exec(select(Playlist).where(Playlist.name == "Unified Playlist")).one()
    assert playlist is not None
    pt = session.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id)).all()
    assert len(pt) == 1


def test_unify_playlists_idempotent(session):
    track_data = _track_data()
    do_import_tracks(session, {str(track_data["Track ID"]): track_data})
    unify_apple_tracks(session)

    pl = _playlist_data(track_ids=[track_data["Track ID"]])
    do_import_playlists(session, [pl])

    first_pl, first_tr = unify_apple_playlists(session)
    session.flush()
    second_pl, second_tr = unify_apple_playlists(session)

    assert first_pl == 1
    assert second_pl == 0
    assert second_tr == 0
