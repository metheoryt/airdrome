"""The durable decision mirror (v2 file after every commit that touched a decision) and the imports."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.models import CanonChoice, PairDecision
from airdrome.normalize.dedup.auto import auto_deduplicate
from airdrome.normalize.dedup.decisions import Canon, dump_v2, key_text, load_v2, pair, track_key
from airdrome.normalize.dedup.mirror import install_mirror, restore_if_empty, write_mirror
from airdrome.normalize.dedup.store import (
    ImportCounts,
    export_decisions,
    import_decisions,
    import_legacy,
    load_pairs,
    stash_mirror_snapshot,
)

from factories import add_canon, add_pair, make_track, old_hash


SAME, DIFF = DecisionKind.SAME, DecisionKind.DIFFERENT
T0 = datetime(2021, 6, 1, tzinfo=UTC)
EMPTY_V2 = {"version": 2, "pairs": [], "canons": []}


@pytest.fixture()
def mirror_path(tmp_path):
    """A mirror path whose parent does not exist yet — the real `.airdrome/` case."""
    return tmp_path / ".airdrome" / "duplicates.json"


def _read(path) -> dict:
    """The mirror file's JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def _two(s):
    """Two tracks a flag set would not join."""
    return make_track(s, "Song", "Artist", "Album A"), make_track(s, "Other", "Band", "Album B")


def _decision(t1, t2, kind=SAME, at=T0, origin=DecisionOrigin.MCP):
    """A Pair about two tracks."""
    return pair(key_text(track_key(t1)), key_text(track_key(t2)), kind, at, origin)


# --- the write half: snapshot on flush, file on commit ---


def test_mirror_written_after_commit(session, mirror_path):
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    add_pair(session, t1, t2, SAME, at=T0)
    add_canon(session, t1, at=T0)

    stash_mirror_snapshot(session)
    assert not mirror_path.exists()  # not before the commit
    session.commit()

    data = _read(mirror_path)
    assert data["version"] == 2
    [p] = data["pairs"]
    assert {tuple(p["a"]), tuple(p["b"])} == {track_key(t1), track_key(t2)}
    assert (p["kind"], p["decided_at"], p["origin"]) == ("same", T0.isoformat(), "mcp")
    assert data["canons"] == [{"track": list(track_key(t1)), "decided_at": T0.isoformat(), "origin": "mcp"}]


def test_rollback_drops_the_snapshot(session, mirror_path):
    """A snapshot left in session.info would be written by the next unrelated commit."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    import_decisions(session, [_decision(t1, t2)], [])
    session.rollback()

    make_track(session, "Unrelated")
    session.commit()

    assert not mirror_path.exists()


def test_clearing_every_decision_writes_empty_v2(session, mirror_path):
    """The guard against resurrection: a stale file would re-seed the next empty DB."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    import_decisions(session, [_decision(t1, t2)], [])
    session.commit()

    session.execute(delete(PairDecision))
    stash_mirror_snapshot(session)
    session.commit()

    assert _read(mirror_path) == EMPTY_V2


def test_commit_that_touched_no_decision_leaves_the_mirror_alone(session, mirror_path):
    install_mirror(session, mirror_path)

    make_track(session, "Unrelated")
    session.commit()

    assert not mirror_path.exists()


def test_write_mirror_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / ".airdrome" / "duplicates.json"

    write_mirror(EMPTY_V2 | {"pairs": [{"a": ["x", None, None, None]}]}, path)
    write_mirror(EMPTY_V2, path)

    assert [p.name for p in path.parent.iterdir()] == ["duplicates.json"]
    assert _read(path) == EMPTY_V2


# --- the read half: restore into empty tables only ---


def test_restore_seeds_empty_tables(session, mirror_path):
    t1, t2 = _two(session)
    write_mirror(
        dump_v2([_decision(t1, t2)], [Canon(key_text(track_key(t2)), T0, DecisionOrigin.MCP)]), mirror_path
    )

    assert restore_if_empty(session, mirror_path) == 2

    [p] = load_pairs(session)
    assert (p.kind, p.decided_at) == (SAME, T0)
    assert session.scalar(select(func.count()).select_from(CanonChoice)) == 1


def test_restore_leaves_non_empty_tables_untouched(session, mirror_path):
    t1, t2 = _two(session)
    t3 = make_track(session, "Third", "Band")
    write_mirror(dump_v2([_decision(t1, t3)], []), mirror_path)
    add_canon(session, t2)  # only the canon table holds a row: still not empty

    assert restore_if_empty(session, mirror_path) == 0
    assert load_pairs(session) == []

    add_pair(session, t1, t2, DIFF)
    assert restore_if_empty(session, mirror_path) == 0
    assert [p.kind for p in load_pairs(session)] == [DIFF]


def test_restore_is_a_noop_without_a_file(session, mirror_path):
    assert restore_if_empty(session, mirror_path) == 0


def test_restore_noop_on_legacy_empty_object(session, mirror_path):
    write_mirror({}, mirror_path)

    assert restore_if_empty(session, mirror_path) == 0


def test_restore_refuses_legacy_file_and_leaves_it_untouched(session, mirror_path):
    """The live mirror is still version-less on the first run after a rebuild: refuse, never overwrite."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    legacy = {"g": {"members": [old_hash(t1), old_hash(t2)], "canon_hashes": [None, old_hash(t1)]}}
    write_mirror(legacy, mirror_path)
    before = mirror_path.read_bytes()

    with pytest.raises(ValueError) as exc:
        restore_if_empty(session, mirror_path)
    assert "dedup-import" in str(exc.value)
    assert "scripts/migrate_dedup_groups.py" in str(exc.value)
    session.commit()  # nothing was stashed, so nothing is written

    assert mirror_path.read_bytes() == before
    assert load_pairs(session) == []


def test_restore_raises_on_an_unparsable_mirror(session, mirror_path):
    mirror_path.parent.mkdir(parents=True)
    mirror_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        restore_if_empty(session, mirror_path)


def test_mirror_round_trip(session, mirror_path):
    """restore → commit must reproduce the file byte for byte, or a DB rebuild would drift it."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    t3 = make_track(session, "Third", "Band")
    pairs = [
        _decision(t1, t2, SAME, T0),
        _decision(t2, t3, DIFF, T0 + timedelta(seconds=1), DecisionOrigin.IMPORT),
    ]
    canons = [Canon(key_text(track_key(t3)), T0 + timedelta(microseconds=7), DecisionOrigin.MIGRATED)]
    write_mirror(dump_v2(pairs, canons), mirror_path)
    original = mirror_path.read_bytes()

    restore_if_empty(session, mirror_path)
    session.commit()

    assert mirror_path.read_bytes() == original


def test_a_wiped_table_recovers_its_decisions_from_the_mirror(session, mirror_path):
    """The whole point: a schema rebuild drops the rows, the mirror puts the decisions back."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    import_decisions(session, [_decision(t1, t2)], [])
    session.commit()

    session.execute(delete(PairDecision))  # stands in for the rebuilt database
    session.commit()

    assert restore_if_empty(session, mirror_path) == 1
    auto_deduplicate(session)
    assert t2.canon_id == t1.id or t1.canon_id == t2.id


# --- imports ---


def test_import_v2_keeps_file_timestamps_and_newest_wins(session):
    t1, t2 = _two(session)
    t3 = make_track(session, "Third", "Band")
    add_pair(session, t1, t2, DIFF, at=T0 + timedelta(days=1))  # newer than the file's row: kept
    add_pair(session, t1, t3, DIFF, at=T0 - timedelta(days=1))  # older than the file's row: replaced

    counts = import_decisions(
        session, [_decision(t1, t2, SAME, T0), _decision(t1, t3, SAME, T0), _decision(t2, t3, SAME, T0)], []
    )

    assert counts == ImportCounts(created=1, replaced=1, kept=1)
    by_ends = {(p.a, p.b): p for p in load_pairs(session)}
    k1, k2, k3 = (key_text(track_key(t)) for t in (t1, t2, t3))
    assert by_ends[tuple(sorted((k1, k2)))].kind == DIFF
    assert by_ends[tuple(sorted((k1, k3)))].kind == SAME
    assert all(p.decided_at == T0 for p in by_ends.values() if p.kind == SAME)


def test_reimport_same_file_is_noop(session):
    t1, t2 = _two(session)
    doc = dump_v2([_decision(t1, t2)], [Canon(key_text(track_key(t1)), T0, DecisionOrigin.MCP)])
    first = import_decisions(session, *load_v2(doc))
    snapshot = export_decisions(session)
    second = import_decisions(session, *load_v2(doc))

    assert first == ImportCounts(created=2, replaced=0, kept=0)
    assert second == ImportCounts(created=0, replaced=0, kept=2)
    assert export_decisions(session) == snapshot


def test_import_rejects_a_pair_listed_twice(session):
    """Two rows for one pair at one time would let file order pick the kind: refused before any write."""
    t1, t2 = _two(session)

    with pytest.raises(ValueError, match="more than once"):
        import_decisions(session, [_decision(t1, t2, SAME), _decision(t2, t1, DIFF)], [])
    with pytest.raises(ValueError, match="more than once"):
        c = Canon(key_text(track_key(t1)), T0, DecisionOrigin.MCP)
        import_decisions(session, [], [c, c])

    assert load_pairs(session) == []


def test_import_clamps_a_future_decided_at_to_now(session):
    t1, t2 = _two(session)
    future = datetime(2100, 1, 1, tzinfo=UTC)

    import_decisions(
        session,
        [_decision(t1, t2, at=future)],
        [Canon(key_text(track_key(t1)), future, DecisionOrigin.IMPORT)],
    )

    now = datetime.now(UTC)
    [p] = load_pairs(session)
    assert p.decided_at <= now
    assert session.scalars(select(CanonChoice)).one().decided_at <= now


def test_import_legacy_file(session):
    """A version-less file: a twin group gives `same`, an all-NULL group `different`, in file order."""
    a = make_track(session, "A", "Band")
    b = make_track(session, "B", "Band")
    c = make_track(session, "C", "Band")
    d = make_track(session, "D", "Band")
    data = {
        "twins": {"members": [old_hash(a), old_hash(b)], "canon_hashes": [None, old_hash(a)]},
        "apart": {
            "members": [old_hash(c), old_hash(d), "gone"],
            "canon_hashes": [None, None, None],
        },
    }
    now = datetime(2026, 10, 19, tzinfo=UTC)  # the past: no clamping in play

    counts, converted = import_legacy(session, data, now)

    assert counts == ImportCounts(created=2, replaced=0, kept=0)
    assert converted.dropped == 1
    rows = {frozenset((p.a, p.b)): p for p in load_pairs(session)}
    key = {t: key_text(track_key(t)) for t in (a, b, c, d)}
    same = rows[frozenset((key[a], key[b]))]
    apart = rows[frozenset((key[c], key[d]))]
    assert (same.kind, apart.kind) == (SAME, DIFF)
    assert same.decided_at == now - timedelta(microseconds=1)
    assert apart.decided_at == now  # the later file entry is the newer decision, the last one is `now`
    assert {same.origin, apart.origin} == {DecisionOrigin.IMPORT}


@pytest.mark.parametrize("mirror_exists", [True, False])
def test_an_import_that_changes_nothing_leaves_the_mirror_alone(session, mirror_path, mirror_exists):
    """A legacy file whose members name no live track creates nothing, so the mirror is not rewritten."""
    install_mirror(session, mirror_path)
    make_track(session, "Live", "Band")
    if mirror_exists:
        write_mirror({"sentinel": True}, mirror_path)
    before = mirror_path.read_bytes() if mirror_exists else None
    data = {"g": {"members": ["gone-1", "gone-2"], "canon_hashes": [None, None]}}

    counts, converted = import_legacy(session, data, datetime(2026, 10, 19, tzinfo=UTC))
    session.commit()

    assert counts == ImportCounts()
    assert converted.dropped == 2
    assert (mirror_path.read_bytes() if mirror_path.exists() else None) == before


def test_a_reimport_that_keeps_every_row_leaves_the_mirror_alone(session, mirror_path):
    """Re-importing what the DB already holds changes nothing, so the mirror keeps its bytes."""
    install_mirror(session, mirror_path)
    t1, t2 = _two(session)
    import_decisions(session, [_decision(t1, t2)], [])
    session.commit()
    write_mirror({"sentinel": True}, mirror_path)
    before = mirror_path.read_bytes()

    assert import_decisions(session, [_decision(t1, t2)], []) == ImportCounts(kept=1)
    session.commit()

    assert mirror_path.read_bytes() == before
