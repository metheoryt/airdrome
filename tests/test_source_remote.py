"""Read-only source playlist remote tests.

`SourcePlaylistRemote` adapts the imported `SourcePlaylist`/`SourceTrack` rows to the
`PlaylistAdapter` interface so the reconcile engine can treat a cloud source as just
another (read-only) remote. These tests pin the read half — listing, ordered refs, and
canonical resolution through the `SourceTrack.track_id` FK — and that the write half is
genuinely closed off.
"""

import pytest

from airdrome.cloud.sources import SourcePlaylist, SourcePlaylistTrack, SourceTrack
from airdrome.enums import Source
from airdrome.playlists.adapter import ExternalTrackRef
from airdrome.playlists.source_remote import SourcePlaylistRemote

from factories import make_track


def _source_track(s, provider: Source, source_id: str, canon: int | None) -> SourceTrack:
    st = SourceTrack(provider=provider, source_id=source_id, title=f"t-{source_id}", track_id=canon)
    s.add(st)
    s.flush()
    return st


def _source_playlist(
    s, provider: Source, source_id: str, members: list[SourceTrack], *, folder: bool = False
) -> SourcePlaylist:
    pl = SourcePlaylist(provider=provider, source_id=source_id, name=f"P-{source_id}", folder=folder)
    s.add(pl)
    s.flush()
    for pos, st in enumerate(members, start=1):
        s.add(SourcePlaylistTrack(playlist_id=pl.id, track_id=st.id, position=pos))
    s.flush()
    return pl


def test_lists_non_folder_playlists_for_its_provider(session):
    """list_playlists scopes to one provider and hides folder containers."""
    t = _source_track(session, Source.APPLE_XML, "100", canon=make_track(session, "a").id)
    _source_playlist(session, Source.APPLE_XML, "p1", [t])
    _source_playlist(session, Source.APPLE_XML, "folder1", [t], folder=True)
    _source_playlist(session, Source.APPLE_MS, "p2", [t])  # different provider

    remote = SourcePlaylistRemote(session, Source.APPLE_XML)
    listed = {p.id for p in remote.list_playlists()}

    assert listed == {"p1"}  # no folder, no other-provider playlist


def test_get_track_refs_in_position_order(session):
    """get_track_refs returns members ordered by position, duplicates preserved."""
    a = _source_track(session, Source.APPLE_XML, "1", canon=make_track(session, "a").id)
    b = _source_track(session, Source.APPLE_XML, "2", canon=make_track(session, "b").id)
    _source_playlist(session, Source.APPLE_XML, "p1", [b, a, b])  # out of insert order, dup b

    remote = SourcePlaylistRemote(session, Source.APPLE_XML)
    refs = remote.get_track_refs("p1")

    assert [remote.to_canonical_track(r) for r in refs] == [b.track_id, a.track_id, b.track_id]


def test_to_canonical_resolves_fk_and_canon_chain(session):
    """to_canonical follows SourceTrack.track_id, then the track's canon hop."""
    canon = make_track(session, "canon")
    twin = make_track(session, "twin", canon_id=canon.id)
    st_unified = _source_track(session, Source.APPLE_XML, "u", canon=twin.id)
    st_orphan = _source_track(session, Source.APPLE_XML, "o", canon=None)  # not unified yet

    remote = SourcePlaylistRemote(session, Source.APPLE_XML)

    # twin resolves up to its canon; an un-unified source track is unresolved (None).
    assert remote.to_canonical_track(ExternalTrackRef(id=str(st_unified.id))) == canon.id
    assert remote.to_canonical_track(ExternalTrackRef(id=str(st_orphan.id))) is None


def test_is_read_only(session):
    """A source remote is not writable and refuses every mutating call."""
    remote = SourcePlaylistRemote(session, Source.APPLE_XML)
    assert remote.writable is False
    ref = ExternalTrackRef(id="1")
    with pytest.raises(NotImplementedError):
        remote.create(object())
    with pytest.raises(NotImplementedError):
        remote.add_track("p1", ref)
    with pytest.raises(NotImplementedError):
        remote.remove_track("p1", ref)
    with pytest.raises(NotImplementedError):
        remote.from_canonical_track(1)
