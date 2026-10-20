import pytest

from airdrome.normalize.dedup.persistence import flatten_canon_chains

from factories import make_track


def test_flatten_collapses_chain(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id  # chain A <- B <- C
    session.flush()

    changed = flatten_canon_chains(session)

    assert changed == 1
    session.refresh(c)
    assert c.canon_id == a.id


def test_flatten_idempotent(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    b.canon_id = a.id
    c.canon_id = b.id
    session.flush()

    flatten_canon_chains(session)
    assert flatten_canon_chains(session) == 0


def test_flatten_no_chains_returns_zero(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    b.canon_id = a.id  # flat: canon is terminal
    session.flush()

    assert flatten_canon_chains(session) == 0


def test_flatten_cycle_asserts(session):
    # A cycle (A↔B) is structurally invalid; the function's `seen` guard
    # prevents an infinite walk, but the post-condition assert must fire
    # because the chain can't be flattened to a terminal canon.
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    a.canon_id = b.id
    b.canon_id = a.id
    session.flush()

    with pytest.raises(AssertionError):
        flatten_canon_chains(session)


def test_flatten_multiple_disconnected_chains(session):
    a = make_track(session, "A", "X")
    b = make_track(session, "B", "X")
    c = make_track(session, "C", "X")
    x = make_track(session, "X1", "Y")
    y = make_track(session, "Y1", "Y")
    z = make_track(session, "Z1", "Y")
    b.canon_id = a.id
    c.canon_id = b.id
    y.canon_id = x.id
    z.canon_id = y.id
    session.flush()

    changed = flatten_canon_chains(session)

    assert changed == 2
    session.refresh(c)
    session.refresh(z)
    assert c.canon_id == a.id
    assert z.canon_id == x.id
