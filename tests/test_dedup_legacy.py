"""Old hash-multiset dedup groups converted to pair decisions (pure, no DB)."""

import importlib.util
import itertools
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, delete, insert, text

from airdrome.cloud import sources  # noqa: F401  (registers SourceTrack so Track's mapper configures)
from airdrome.conf import settings
from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.models import Track
from airdrome.normalize.dedup.decisions import Canon, Pair, key_text, load_v2, pair, resolve
from airdrome.normalize.dedup.legacy import (
    LEGACY,
    LegacyGroup,
    Migration,
    convert_group,
    format_report,
    legacy_groups,
    legacy_hash,
    live_tracks,
    migrate,
    read_legacy_groups,
    write_migration,
)

from factories import make_track, old_hash


AT = datetime(2026, 10, 18, tzinfo=UTC)
ORIGIN = DecisionOrigin.MIGRATED
SAME, DIFF = DecisionKind.SAME, DecisionKind.DIFFERENT


def k(name: str) -> str:
    """Return the key text of a track titled `name`."""
    return key_text((name, None, None, None))


def first(keys: list[str]) -> str:
    """Pick the first key (stands in for canon_order)."""
    return keys[0]


def last(keys: list[str]) -> str:
    """Pick the last key (a canon_order choice that differs from the stored canon)."""
    return keys[-1]


def p(x: str, y: str, kind: DecisionKind) -> Pair:
    """Expected migrated pair at AT."""
    return pair(x, y, kind, AT, ORIGIN)


def run(group: LegacyGroup, live: dict[str, list[str]], pick=first):
    """Convert `group` with the shared AT and origin."""
    return convert_group(group, live, pick, AT, ORIGIN)


def by_kind(pairs: list[Pair], kind: DecisionKind) -> set[tuple[str, str]]:
    """Return the (a, b) ends of pairs of one kind."""
    return {(x.a, x.b) for x in pairs if x.kind == kind}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "title": "Café",
                "artist": "A",
                "album_artist": "AA",
                "album": "Al",
                "track_n": 3,
                "disc_n": 1,
                "year": 1999,
                "duration": 203,
            },
            "Café;A;AA;Al;3;1;1999;205",
        ),
        ({"title": "t"}, "t;;;;;;;"),
    ],
)
def test_legacy_hash_format(fields: dict, expected: str) -> None:
    """The frozen formula, pinned to the values the removed `Track.duplicate_hash` produced."""
    args = {
        n: fields.get(n)
        for n in ("title", "artist", "album_artist", "album", "track_n", "disc_n", "year", "duration")
    }
    assert legacy_hash(**args) == expected


def test_convert_twin_group() -> None:
    """A twin gets one same(twin, canon) and nothing is different."""
    g = LegacyGroup("x", [("h1", None), ("h2", "h1")])
    r = run(g, {"h1": [k("1")], "h2": [k("2")]})
    assert r.pairs == [p(k("2"), k("1"), SAME)]
    assert r.canons == [] and r.dropped == 0 and r.ambiguous == 0


def test_convert_all_null_group_is_all_different() -> None:
    """Reviewed-not-duplicates: every pair of three hashes is different."""
    g = LegacyGroup("x", [("h1", None), ("h2", None), ("h3", None)])
    r = run(g, {"h1": [k("1")], "h2": [k("2")], "h3": [k("3")]})
    assert by_kind(r.pairs, SAME) == set()
    assert len(r.pairs) == 3 and all(x.kind == DIFF for x in r.pairs)


def test_convert_forest_group() -> None:
    """Two roots and a twin of the first: one same, two different."""
    g = LegacyGroup("x", [("r1", None), ("r2", None), ("t", "r1")])
    r = run(g, {"r1": [k("r1")], "r2": [k("r2")], "t": [k("t")]})
    assert r.pairs and by_kind(r.pairs, SAME) == {tuple(sorted((k("t"), k("r1"))))}
    assert by_kind(r.pairs, DIFF) == {
        tuple(sorted((k("r1"), k("r2")))),
        tuple(sorted((k("t"), k("r2")))),
    }


def test_convert_drops_dead_members() -> None:
    """A member with no live track is dropped; one whose canon is dead is kept, vetoes intact."""
    g = LegacyGroup("x", [("live", None), ("dead", None), ("orphan", "gone"), ("other", None)])
    live = {"live": [k("live")], "orphan": [k("orphan")], "other": [k("other")]}
    r = run(g, live)
    assert r.dropped == 1
    assert r.dangling_canon == 1
    assert by_kind(r.pairs, SAME) == set()
    assert by_kind(r.pairs, DIFF) == {
        tuple(sorted(e))
        for e in [(k("live"), k("orphan")), (k("live"), k("other")), (k("orphan"), k("other"))]
    }
    assert r.canons == []


def test_convert_skips_self_canon_pair() -> None:
    """A member whose canon hash is its own hash emits no pair(k, k)."""
    g = LegacyGroup("x", [("h1", "h1"), ("h2", "h1")])
    r = run(g, {"h1": [k("1")], "h2": [k("2")]})
    assert r.pairs == [p(k("2"), k("1"), SAME)]


def test_convert_ambiguous_hash_expands_to_all_keys() -> None:
    """A hash with two live keys counts once and joins every key to the canon."""
    g = LegacyGroup("x", [("h1", None), ("h2", "h1")])
    r = run(g, {"h1": [k("1")], "h2": [k("2a"), k("2b")]})
    assert r.ambiguous == 1
    assert by_kind(r.pairs, SAME) == {tuple(sorted((k("2a"), k("1")))), tuple(sorted((k("2b"), k("1"))))}


def test_canon_choice_only_where_a_person_picked_it() -> None:
    """MCP groups and canon == pick yield no Canon; a differing pick yields one for the stored canon."""
    live = {"h1": [k("1")], "h2": [k("2")]}
    members = [("h1", None), ("h2", "h1")]
    assert run(LegacyGroup("mcp:3:9", members), live, last).canons == []
    assert run(LegacyGroup("tui", members), live, first).canons == []
    assert run(LegacyGroup("tui", members), live, last).canons == [Canon(k("1"), AT, ORIGIN)]
    assert run(LegacyGroup(None, members), live, last).canons == [Canon(k("1"), AT, ORIGIN)]


def test_every_decision_carries_at_and_origin() -> None:
    """All pairs and canons from a group share `at` and the origin."""
    g = LegacyGroup("x", [("h1", None), ("h2", "h1"), ("h3", None)])
    r = run(g, {"h1": [k("1")], "h2": [k("2")], "h3": [k("3")]}, last)
    assert r.pairs and r.canons
    assert {x.decided_at for x in [*r.pairs, *r.canons]} == {AT}
    assert {x.origin for x in [*r.pairs, *r.canons]} == {ORIGIN}


def test_convert_rejects_naive_at() -> None:
    """A naive `at` is a ValueError."""
    g = LegacyGroup("x", [("h1", None), ("h2", "h1")])
    with pytest.raises(ValueError):
        convert_group(g, {"h1": [k("1")], "h2": [k("2")]}, first, datetime(2026, 1, 1), ORIGIN)


def test_legacy_groups_keep_file_order() -> None:
    """Groups come back in file order with (member, canon) pairs; {} gives []."""
    data = {
        "zz": {"members": ["a", "b"], "canon_hashes": [None, "a"]},
        "aa": {"members": ["c"], "canon_hashes": [None]},
    }
    got = legacy_groups(data)
    assert [g.label for g in got] == ["zz", "aa"]
    assert got[0].members == [("a", None), ("b", "a")]
    assert legacy_groups({}) == []


def test_shared_hash_root_and_self_canon_order_independent() -> None:
    """("h", None) and ("h", "h") pair h's two keys, whichever comes first."""
    live = {"h": [k("a"), k("b")]}
    one = run(LegacyGroup("x", [("h", None), ("h", "h")]), live)
    two = run(LegacyGroup("x", [("h", "h"), ("h", None)]), live)
    assert one == two
    assert one.pairs == [p(k("b"), k("a"), SAME)]


def test_shared_hash_with_other_canon_order_independent() -> None:
    """("h", None) and ("h", "r") with two keys for h: both join r, none inverted to different."""
    live = {"h": [k("a"), k("b")], "r": [k("r")]}
    one = run(LegacyGroup("x", [("r", None), ("h", None), ("h", "r")]), live)
    two = run(LegacyGroup("x", [("h", "r"), ("h", None), ("r", None)]), live)
    assert one == two
    assert by_kind(one.pairs, SAME) == {tuple(sorted((k("a"), k("r")))), tuple(sorted((k("b"), k("r"))))}
    assert by_kind(one.pairs, DIFF) == set()


def test_conflicting_canons_union_and_counter() -> None:
    """A hash naming two canons joins both and is counted."""
    live = {"h": [k("h")], "r1": [k("r1")], "r2": [k("r2")]}
    r = run(LegacyGroup("x", [("h", "r1"), ("h", "r2"), ("r1", None), ("r2", None)]), live)
    assert r.conflicting_canons == 1
    assert by_kind(r.pairs, SAME) == {tuple(sorted((k("h"), k("r1")))), tuple(sorted((k("h"), k("r2"))))}
    assert by_kind(r.pairs, DIFF) == {tuple(sorted((k("r1"), k("r2"))))}


def test_dead_hash_listed_twice_counts_once() -> None:
    """A dead hash repeated in the group is one drop."""
    g = LegacyGroup("x", [("dead", None), ("dead", None), ("a", None)])
    assert run(g, {"a": [k("a")]}).dropped == 1


def test_any_member_order_gives_same_result() -> None:
    """Every permutation of a mixed group converts identically."""
    members = [("r", None), ("h", None), ("h", "r"), ("h", "h"), ("o", "gone"), ("x", None)]
    live = {"r": [k("r")], "h": [k("a"), k("b")], "o": [k("o")], "x": [k("x")]}
    results = {
        repr(run(LegacyGroup("tui", list(perm)), live, last)) for perm in itertools.permutations(members)
    }
    assert len(results) == 1


@pytest.mark.parametrize(
    "bad", [{"g": {"members": ["a"]}}, {"g": None}, {"g": {"members": ["a"], "canon_hashes": []}}]
)
def test_legacy_groups_reject_malformed(bad: dict) -> None:
    """A malformed entry is a ValueError, not a KeyError or TypeError."""
    with pytest.raises(ValueError):
        legacy_groups(bad)


# --- DB half: reading the live group tables and migrating them -------------------------------------

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture()
def conn(session) -> Connection:
    """The test session's connection with the legacy tables in place."""
    c = session.connection()
    LEGACY.create_all(c, checkfirst=True)
    return c


def store(conn: Connection, groups: dict[int, tuple[str | None, list[tuple[Track, Track | None]]]]) -> None:
    """Insert old-style groups with explicit ids: {id: (label, [(member, canon | None)])}."""
    gt, mt = LEGACY.tables["dedupgroup"], LEGACY.tables["dedupgroupmember"]
    conn.execute(delete(mt))
    conn.execute(delete(gt))
    for gid, (label, members) in groups.items():
        conn.execute(insert(gt).values(id=gid, label=label))
        for m, c in members:
            conn.execute(
                insert(mt).values(
                    group_id=gid,
                    member_hash=old_hash(m),
                    canon_hash=old_hash(c) if c is not None else None,
                )
            )


def components(m: Migration, conn: Connection) -> list[frozenset[int]]:
    """Resolve a migration's output against the live tracks and return the components."""
    return resolve(m.pairs, m.canons, live_tracks(conn).id_of_key).components


def test_read_legacy_groups_in_id_order(session, conn) -> None:
    """Groups come back in id order with their (member, canon) hash pairs."""
    a, b = make_track(session, "a"), make_track(session, "b")
    store(conn, {7: ("late", [(a, None)]), 3: (None, [(b, a)])})
    got = read_legacy_groups(conn)
    assert [gid for gid, _ in got] == [3, 7]
    assert got[0][1] == LegacyGroup(None, [(old_hash(b), old_hash(a))])


@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
def test_migrate_repro_is_order_independent(session, conn, order) -> None:
    """w->v and x->w end up as one component {v, w, x} whichever group id comes first."""
    v, w, x = (make_track(session, n) for n in "vwx")
    g_wv = ("tui", [(w, v), (v, None)])
    g_xw = ("tui", [(x, w), (w, None)])
    store(conn, {order[0]: g_wv, order[1]: g_xw})
    m = migrate(conn, NOW)
    assert components(m, conn) == [frozenset({v.id, w.id, x.id})]


def test_migrate_newest_group_wins_contradiction(session, conn) -> None:
    """An older all-NULL group and a newer b->a group contradict; the newer joins a and b."""
    a, b = make_track(session, "a"), make_track(session, "b")
    store(conn, {1: ("tui", [(a, None), (b, None)]), 2: ("tui", [(b, a), (a, None)])})
    m = migrate(conn, NOW)
    assert [c.kind for c in m.clusters] == ["contradiction"]
    assert m.clusters[0].group_ids == [1, 2]
    assert components(m, conn) == [frozenset({a.id, b.id})]
    assert "group 1: different" in m.clusters[0].text  # the older group's row, replaced by the newer


def test_migrate_timestamps_increase_with_group_id(session, conn) -> None:
    """Every decided_at is before `now`, and a later group id gets a later timestamp."""
    a, b, c, d = (make_track(session, n) for n in "abcd")
    store(conn, {5: ("tui", [(a, None), (b, None)]), 9: ("tui", [(c, None), (d, None)])})
    m = migrate(conn, NOW)
    assert all(x.decided_at < NOW for x in [*m.pairs, *m.canons])
    first = next(p for p in m.pairs if p.a == k("a") or p.b == k("a"))
    second = next(p for p in m.pairs if p.a == k("c") or p.b == k("c"))
    assert first.decided_at < second.decided_at < NOW


def test_migrate_canon_choice_rule(session, conn) -> None:
    """Only a TUI group whose canon differs from canon_order's pick records a Canon."""
    t = [make_track(session, f"t{i}") for i in range(1, 7)]
    store(
        conn,
        {
            1: ("mcp:1", [(t[0], None), (t[1], t[0])]),  # MCP: never a canon
            2: ("tui", [(t[2], None), (t[3], t[2])]),  # canon == canon_order's pick
            3: ("tui", [(t[4], t[5]), (t[5], None)]),  # canon != pick (t5 is older)
        },
    )
    m = migrate(conn, NOW)
    assert [c.track for c in m.canons] == [k("t6")]


def test_migrate_counts_dropped_members(session, conn) -> None:
    """A member hash with no live track is dropped and counted."""
    a = make_track(session, "a")
    ghost = Track(title="ghost")
    store(conn, {1: ("tui", [(a, None), (ghost, None)])})
    m = migrate(conn, NOW)
    assert (m.groups, m.dropped) == (1, 1)


def test_migrate_counts_shared_keys(session, conn) -> None:
    """Two live tracks with one natural key (NULL album) are counted as a shared key.

    The live DB being migrated predates NULLS NOT DISTINCT, so the test drops the key constraint
    (transactional DDL, undone at teardown) to reproduce it.
    """
    # Constraint name as Postgres derives it from Track.__table_args__ (airdrome/models.py).
    conn.execute(text("ALTER TABLE track DROP CONSTRAINT track_title_artist_album_album_artist_key"))
    make_track(session, "dup", artist="x", year=1)
    make_track(session, "dup", artist="x", year=2)
    store(conn, {})
    assert migrate(conn, NOW).shared_keys == 1


def test_format_report_totals(session, conn) -> None:
    """The report lists the groups and ends with totals including cluster kinds."""
    a, b = make_track(session, "a"), make_track(session, "b")
    store(conn, {1: ("tui", [(a, None), (b, None)]), 2: ("tui", [(b, a), (a, None)])})
    report = format_report(migrate(conn, NOW))
    assert "contradiction" in report
    assert report.rstrip().splitlines()[-1].startswith("clusters")


def test_write_migration_writes_v2_and_refuses_existing_out(session, conn, tmp_path, monkeypatch) -> None:
    """The output loads as v2; an existing out or the live mirror path is refused."""
    a, b = make_track(session, "a"), make_track(session, "b")
    store(conn, {1: ("tui", [(a, None), (b, None)])})
    out = tmp_path / "v2.json"
    write_migration(conn, out, NOW)
    pairs, canons = load_v2(json.loads(out.read_text()))
    assert len(pairs) == 1 and canons == []
    with pytest.raises(ValueError, match="already exists"):
        write_migration(conn, out, NOW)
    # A stand-in live mirror that does not exist, so only the path-equality check can refuse it.
    live = tmp_path / "live" / "duplicates.json"
    monkeypatch.setattr(settings, "duplicates_filepath", live)
    with pytest.raises(ValueError, match="live mirror path"):
        write_migration(conn, live, NOW)
    assert not live.exists()


def _load_script():
    """Import scripts/migrate_dedup_groups.py as a module."""
    path = Path(__file__).parent.parent / "scripts" / "migrate_dedup_groups.py"
    spec = importlib.util.spec_from_file_location("migrate_dedup_groups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_refuses_live_mirror_path(monkeypatch, tmp_path, capsys) -> None:
    """The live mirror path is refused before any connection is made."""
    module = _load_script()

    def boom(*args, **kwargs):
        raise AssertionError("connected")

    monkeypatch.setattr(module, "create_engine", boom)
    live = tmp_path / "live" / "duplicates.json"  # non-existent stand-in: only path equality refuses
    monkeypatch.setattr(settings, "duplicates_filepath", live)
    assert module.main([str(live)]) != 0
    assert "live mirror path" in capsys.readouterr().err


def test_script_runs_read_only_and_never_commits(session, conn, tmp_path, monkeypatch, capsys) -> None:
    """The script's one transaction is read-only and rolled back; the report is printed."""
    a, b = make_track(session, "a"), make_track(session, "b")
    store(conn, {1: ("tui", [(a, None), (b, None)])})
    module = _load_script()
    seen: list[str] = []

    class Engine:
        """Hands out the test connection, recording commit/rollback instead of acting."""

        def connect(self):
            return self

        def __enter__(self):
            return Spy()

        def __exit__(self, *exc):
            return False

        def dispose(self):
            pass

    class Spy:
        """Delegates to the test connection but swallows commit/rollback and logs them."""

        def execute(self, *args, **kwargs):
            seen.append(str(args[0]))
            return conn.execute(*args, **kwargs)

        def commit(self):
            seen.append("COMMIT")

        def rollback(self):
            seen.append("ROLLBACK")

    monkeypatch.setattr(module, "create_engine", lambda *a, **k: Engine())
    out = tmp_path / "out.json"
    assert module.main([str(out), "--dsn", "postgresql://x/y"]) == 0
    assert seen[0] == "SET TRANSACTION READ ONLY"
    assert "COMMIT" not in seen and seen[-1] == "ROLLBACK"
    assert out.exists() and "groups" in capsys.readouterr().out


def test_script_works_in_a_fresh_process(test_engine, tmp_path) -> None:
    """A standalone run configures every mapper the canon query needs (SourceTrack is not imported by
    the script itself); runs against the test DB with rows committed and removed afterwards."""
    track, gt, mt = Track.__table__, LEGACY.tables["dedupgroup"], LEGACY.tables["dedupgroupmember"]
    out = tmp_path / "fresh.json"
    with test_engine.begin() as c:
        LEGACY.create_all(c, checkfirst=True)
        ids = [c.execute(insert(track).values(title=t).returning(track.c.id)).scalar_one() for t in "ab"]
        gid = c.execute(insert(gt).values(label="tui").returning(gt.c.id)).scalar_one()
        c.execute(insert(mt).values(group_id=gid, member_hash="b;;;;;;;", canon_hash="a;;;;;;;"))
        c.execute(insert(mt).values(group_id=gid, member_hash="a;;;;;;;", canon_hash=None))
    try:
        dsn = test_engine.url.render_as_string(hide_password=False)
        script = Path(__file__).parent.parent / "scripts" / "migrate_dedup_groups.py"
        run = subprocess.run(
            [sys.executable, str(script), str(out), "--dsn", dsn], capture_output=True, text=True
        )
        assert run.returncode == 0, run.stderr[-800:]
        assert out.exists()
    finally:
        with test_engine.begin() as c:
            c.execute(delete(mt).where(mt.c.group_id == gid))
            c.execute(delete(gt).where(gt.c.id == gid))
            c.execute(delete(track).where(track.c.id.in_(ids)))
