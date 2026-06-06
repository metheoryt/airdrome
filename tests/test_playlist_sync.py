"""Playlist sync engine tests.

Exercise the backend-agnostic merge in `airdrome.playlists.sync` through an
in-memory `FakeBackend`, so duplication/idempotency behaviour can be asserted
without a real Navidrome SQLite file. The regression these guard against:
synced playlists inflating to ~100x their real size, one extra copy per run.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select

from airdrome.enums import Source
from airdrome.models import Backend, Playlist, PlaylistLink, PlaylistTrack
from airdrome.playlists.adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter
from airdrome.playlists.sync import _dedup, _sync_pair, _three_way_merge

from factories import make_track


class FakeBackend(PlaylistAdapter):
    """Dict-backed PlaylistAdapter with fully controllable canon<->ref mapping.

    `canon_of` (to_canonical) and `ref_of` (from_canonical) are set
    independently so a *non-invertible* round-trip can be modelled directly.
    """

    backend = Backend.NAVIDROME

    def __init__(self):
        self.tracks: dict[str, list[str]] = {}  # ext_id -> ref ids, in order, dups allowed
        self.names: dict[str, str] = {}
        self.canon_of: dict[str, int] = {}  # ref_id -> canonical Track id
        self.ref_of: dict[int, str] = {}  # canonical Track id -> ref_id
        self._counter = 0

    # test wiring helpers ----------------------------------------------------
    def register(self, canon: int, ref_id: str) -> None:
        self.canon_of[ref_id] = canon
        self.ref_of[canon] = ref_id

    def seed(self, ext_id: str, name: str, rows: list[str] | None = None) -> ExternalPlaylist:
        self.tracks[ext_id] = list(rows or [])
        self.names[ext_id] = name
        return ExternalPlaylist(id=ext_id, name=name)

    # PlaylistAdapter interface ---------------------------------------------
    def list_playlists(self):
        return [ExternalPlaylist(id=i, name=n) for i, n in self.names.items()]

    def get(self, external_id):
        if external_id in self.tracks:
            return ExternalPlaylist(id=external_id, name=self.names[external_id])
        return None

    def create(self, playlist):
        self._counter += 1
        return self.seed(f"ext{self._counter}", playlist.name)

    def get_track_refs(self, external_id):
        return [ExternalTrackRef(id=r) for r in self.tracks[external_id]]

    def add_track(self, external_id, ref):
        self.tracks[external_id].append(ref.id)

    def remove_track(self, external_id, ref):
        self.tracks[external_id] = [r for r in self.tracks[external_id] if r != ref.id]

    def to_canonical_track(self, ref):
        return self.canon_of.get(ref.id)

    def from_canonical_track(self, track_id):
        rid = self.ref_of.get(track_id)
        return ExternalTrackRef(id=rid) if rid is not None else None


def _playlist(s, tracks: list) -> Playlist:
    """Create an Airdrome playlist holding `tracks` in order (duplicates allowed)."""
    pl = Playlist(name="P", platform=Source.NAVIDROME, source_id=f"src-{id(tracks)}")
    s.add(pl)
    s.flush()
    for pos, t in enumerate(tracks, start=1):
        s.add(PlaylistTrack(playlist_id=pl.id, track_id=t.id, position=pos))
    s.flush()
    return pl


def _link(s, pl: Playlist, ext_id: str):
    return s.scalars(
        select(PlaylistLink).where(
            PlaylistLink.playlist_id == pl.id, PlaylistLink.backend == Backend.NAVIDROME
        )
    ).one_or_none()


def _pt_count(s, pl: Playlist) -> int:
    return s.scalar(select(func.count()).select_from(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id))


# ── pure helpers ───────────────────────────────────────────────────────────


def test_dedup_preserves_first_seen_order():
    assert _dedup([3, 1, 3, 2, 1, 3]) == [3, 1, 2]


def test_three_way_merge_dedups_result():
    # ours carries duplicates; merged must not.
    assert _dedup(_three_way_merge([], [1, 1, 2], [2, 3])) == [1, 2, 3]


# ── engine behaviour ───────────────────────────────────────────────────────


def test_initial_push_then_idempotent(session):
    """First sync pushes the list; a second identical sync changes nothing."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P")
    pl = _playlist(session, [t1, t2])

    assert _sync_pair(session, be, pl, ext, link=None) is True
    assert be.tracks["e"] == ["r1", "r2"]

    link = _link(session, pl, "e")
    assert _sync_pair(session, be, pl, ext, link=link) is False
    assert be.tracks["e"] == ["r1", "r2"]  # no growth


def test_collapses_pre_existing_backend_duplicates(session):
    """A backend playlist already bloated with N copies collapses to one each."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P", rows=["r1"] * 50 + ["r2"] * 50)
    pl = _playlist(session, [t1, t2])

    _sync_pair(session, be, pl, ext, link=None)

    assert sorted(be.tracks["e"]) == ["r1", "r2"]


def test_collapses_pre_existing_airdrome_duplicates(session):
    """A bloated Airdrome playlisttrack table is rewritten down to distinct tracks."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P")
    pl = _playlist(session, [t1] * 80 + [t2] * 80)
    assert _pt_count(session, pl) == 160

    _sync_pair(session, be, pl, ext, link=None)

    assert _pt_count(session, pl) == 2


def test_non_invertible_mapping_is_stable(session):
    """from_canonical(t)->r but to_canonical(r)->other: must not re-add every run.

    This is the exact shape that drove the ~100x inflation — the added ref read
    back as a *different* canon, so the track looked perpetually missing.
    """
    t = make_track(session, "a")
    other = make_track(session, "z")  # a real, distinct canon the ref maps back to
    be = FakeBackend()
    be.ref_of[t.id] = "r1"  # we add r1 for t...
    be.canon_of["r1"] = other.id  # ...but r1 reads back as `other`
    ext = be.seed("e", "P")
    pl = _playlist(session, [t])

    link = None
    for _ in range(3):
        _sync_pair(session, be, pl, ext, link=link)
        link = _link(session, pl, "e")

    assert be.tracks["e"] == ["r1"]  # exactly one copy after three runs, never growing


def test_pulls_backend_only_addition_into_airdrome(session):
    """A track the backend gained since last sync is merged into Airdrome."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P", rows=["r1", "r2"])  # last sync left r1; user added r2
    pl = _playlist(session, [t1])
    # Last sync settled on just t1, so t2 reads as a genuine backend-side add.
    session.add(
        PlaylistLink(
            playlist_id=pl.id,
            backend=Backend.NAVIDROME,
            external_id="e",
            synced_track_ids=[t1.id],
            synced_at=datetime.now(UTC),
        )
    )
    session.flush()
    link = _link(session, pl, "e")

    _sync_pair(session, be, pl, ext, link=link)

    airdrome_ids = set(
        session.scalars(select(PlaylistTrack.track_id).where(PlaylistTrack.playlist_id == pl.id))
    )
    assert airdrome_ids == {t1.id, t2.id}
    assert sorted(be.tracks["e"]) == ["r1", "r2"]


def test_reset_rebuilds_backend_from_airdrome(session):
    """`reset` drops backend-only rows and rebuilds purely from Airdrome's list."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    # Backend bloated + carrying orphan refs Airdrome doesn't know about.
    ext = be.seed("e", "P", rows=["r1"] * 10 + ["orphan", "orphan", "r2"])
    pl = _playlist(session, [t1, t2])
    session.add(
        PlaylistLink(
            playlist_id=pl.id,
            backend=Backend.NAVIDROME,
            external_id="e",
            synced_track_ids=[t1.id, t2.id],
            synced_at=datetime.now(UTC),
        )
    )
    session.flush()
    link = _link(session, pl, "e")

    _sync_pair(session, be, pl, ext, link=link, reset=True)

    assert sorted(be.tracks["e"]) == ["r1", "r2"]  # orphan gone, dups collapsed
