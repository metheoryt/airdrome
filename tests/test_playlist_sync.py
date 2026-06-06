"""Playlist sync engine tests.

Exercise the backend-agnostic merge in `airdrome.playlists.sync` through an
in-memory `FakeBackend`, so multiplicity/idempotency behaviour can be asserted
without a real Navidrome SQLite file. Two things these guard against:
the runaway duplication that inflated synced playlists to ~100x their size, and
the inverse over-correction (silently stripping a playlist's intentional dupes).
"""

from datetime import UTC, datetime

from sqlalchemy import select

from airdrome.enums import Source
from airdrome.models import Backend, Playlist, PlaylistLink, PlaylistTrack
from airdrome.playlists.adapter import ExternalPlaylist, ExternalTrackRef, PlaylistAdapter
from airdrome.playlists.sync import _sync_pair, _three_way_merge

from factories import make_track


class FakeBackend(PlaylistAdapter):
    """Dict-backed PlaylistAdapter with fully controllable canon<->ref mapping.

    `canon_of` (to_canonical) and `ref_of` (from_canonical) are set
    independently so a *non-invertible* round-trip can be modelled directly.
    Playlist rows are a plain list, so multiplicity is observable.
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
    """Create a playlist holding `tracks` in order (duplicates allowed)."""
    pl = Playlist(name="P", platform=Source.SPOTIFY, source_id=f"src-{id(tracks)}")
    s.add(pl)
    s.flush()
    for pos, t in enumerate(tracks, start=1):
        s.add(PlaylistTrack(playlist_id=pl.id, track_id=t.id, position=pos))
    s.flush()
    return pl


def _link(s, pl: Playlist):
    return s.scalars(
        select(PlaylistLink).where(
            PlaylistLink.playlist_id == pl.id, PlaylistLink.backend == Backend.NAVIDROME
        )
    ).one_or_none()


def _seed_link(s, pl: Playlist, ext_id: str, synced: list[int]) -> None:
    s.add(
        PlaylistLink(
            playlist_id=pl.id,
            backend=Backend.NAVIDROME,
            external_id=ext_id,
            synced_track_ids=synced,
            synced_at=datetime.now(UTC),
        )
    )
    s.flush()


def _pt_rows(s, pl: Playlist) -> list[int]:
    return list(
        s.scalars(
            select(PlaylistTrack.track_id)
            .where(PlaylistTrack.playlist_id == pl.id)
            .order_by(PlaylistTrack.position)
        )
    )


def _never(*_a, **_k):
    raise AssertionError("make_ext should not have been called")


# ── multiset merge ─────────────────────────────────────────────────────────


def test_merge_additive_with_base_applies_both_deltas():
    # base says one copy synced; ours added a 2nd; theirs unchanged -> two copies.
    assert _three_way_merge([1, 2], [1, 1, 2], [1, 2]) == [1, 1, 2]
    # theirs removed its only copy of 2 -> 2 drops out entirely.
    assert _three_way_merge([1, 2], [1, 2], [1]) == [1]


def test_merge_unions_by_max_when_no_base():
    # First sync of a pair: can't tell adds from shared history, so union by count.
    assert _three_way_merge([], [1, 1], [1, 2]) == [1, 1, 2]


# ── engine behaviour ───────────────────────────────────────────────────────


def test_initial_push_then_idempotent(session):
    """First sync pushes the list; a second identical sync changes nothing."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P")
    pl = _playlist(session, [t1, t2])

    assert _sync_pair(session, be, pl, ext, None, _never) is True
    assert be.tracks["e"] == ["r1", "r2"]

    assert _sync_pair(session, be, pl, ext, _link(session, pl), _never) is False
    assert be.tracks["e"] == ["r1", "r2"]  # no growth


def test_intentional_duplicates_are_mirrored_and_stable(session):
    """A track listed twice in Airdrome reaches Navidrome twice and stays put."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P")
    pl = _playlist(session, [t1, t1, t2])  # deliberate dup of t1

    _sync_pair(session, be, pl, ext, None, _never)
    assert be.tracks["e"] == ["r1", "r1", "r2"]

    # Idempotent: the dup is preserved, not collapsed, and nothing grows.
    assert _sync_pair(session, be, pl, ext, _link(session, pl), _never) is False
    assert be.tracks["e"] == ["r1", "r1", "r2"]
    assert _pt_rows(session, pl) == [t1.id, t1.id, t2.id]  # Airdrome untouched too


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

    for _ in range(3):
        _sync_pair(session, be, pl, ext, _link(session, pl), _never)

    assert be.tracks["e"] == ["r1"]  # exactly one copy after three runs, never growing


def test_pulls_backend_only_addition_into_airdrome(session):
    """A track the backend gained since last sync is merged into Airdrome."""
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")
    be.register(t2.id, "r2")
    ext = be.seed("e", "P", rows=["r1", "r2"])  # last sync left r1; user added r2
    pl = _playlist(session, [t1])
    _seed_link(session, pl, "e", [t1.id])  # base: only t1 was synced

    _sync_pair(session, be, pl, ext, _link(session, pl), _never)

    assert set(_pt_rows(session, pl)) == {t1.id, t2.id}
    assert sorted(be.tracks["e"]) == ["r1", "r2"]


def test_missing_file_track_preserved_not_deleted(session):
    """A track with no backend representation stays in Airdrome and is never pushed.

    Across a sync-back it must not read as a one-sided delete — it simply waits
    for its file to appear.
    """
    t1, t2 = make_track(session, "a"), make_track(session, "b")
    be = FakeBackend()
    be.register(t1.id, "r1")  # t2 has no ref — its file is "missing"
    pl = _playlist(session, [t1, t2])

    # First sync creates the backend playlist with only t1; t2 stays Airdrome-only.
    _sync_pair(session, be, pl, ext=None, link=None, make_ext=lambda: be.create(pl))
    ext_id = _link(session, pl).external_id
    assert be.tracks[ext_id] == ["r1"]
    assert _pt_rows(session, pl) == [t1.id, t2.id]  # t2 kept

    # Sync back: t2 is absent from the snapshot/backend but must not be deleted.
    _sync_pair(session, be, pl, be.get(ext_id), _link(session, pl), _never)
    assert be.tracks[ext_id] == ["r1"]
    assert _pt_rows(session, pl) == [t1.id, t2.id]  # still kept, never pushed


def test_no_empty_backend_playlist_created(session):
    """A playlist with nothing representable spawns no backend playlist or link."""
    t = make_track(session, "a")  # no ref registered -> from_canonical returns None
    be = FakeBackend()
    pl = _playlist(session, [t])

    had_changes = _sync_pair(session, be, pl, ext=None, link=None, make_ext=lambda: be.create(pl))

    assert had_changes is False
    assert be.tracks == {}  # create() never fired
    assert _link(session, pl) is None
