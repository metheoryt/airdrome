"""Acceptance harness for the identity ladder (docs/design/identity-ladder.md, *Acceptance*).

Not shipped in the package: a one-off measuring tool, kept so the numbers in the design doc can be
reproduced. Three subcommands, each taking a SQLAlchemy DSN from `$ACCEPTANCE_DSN` (preferred: argv
is visible in `ps`, and the DSN carries the DB password) or `--dsn`; never hard-coded:

- `snapshot` — read-only: every bound file's `source_path` -> sorted paths of all files in its
  dedup group, written to JSON. Run against the live library *before* rebuilding.
- `calibrate` — the similarity distribution of fingerprint pairs whose files carry equal tag
  recording ids vs pairs with different ids that the fingerprint index proposes; used to set
  `FP_MATCH`.
- `compare` — against a rebuilt DB: all files bound, each file's new group ⊇ its snapshot group,
  grown groups with the rung that grew them, method distribution per binding kind, and how many
  stored (hand-made) dedup groups are only partly present. Exits 1 when a file is unbound, missing
  from or extra to the rebuilt DB, or lost a group member. Rung attribution is *approximate*: it
  reports the twin-link methods present in a grown group (recording rungs first), not the exact
  edge that joined each snapshot group.

Usage: `ACCEPTANCE_DSN=... uv run python scripts/identity_acceptance.py <cmd> [args]`.
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session

import airdrome.cloud.sources  # noqa: F401  (registers SourceTrack etc., which Track's relationships name)
from airdrome.library.fingerprint import FP_MATCH, FingerprintIndex, similarity
from airdrome.models import DedupGroupMember, Track


# A dedup group is every file whose track resolves to the same root (canon_id is terminal: one hop).
_GROUPS_SQL = """
    SELECT f.source_path, COALESCE(t.canon_id, t.id) AS root
    FROM trackfile f JOIN track t ON t.id = f.track_id
"""


# Twin-link methods only the identity ladder produces.
_RECORDING_RUNGS = {"MBID", "ACOUSTID", "FINGERPRINT"}


def _engine(dsn: str | None) -> Engine:
    """Engine for $ACCEPTANCE_DSN, else `dsn` (--dsn shows in `ps`); exits when neither is given."""
    dsn = os.environ.get("ACCEPTANCE_DSN") or dsn
    if not dsn:
        sys.exit("set ACCEPTANCE_DSN (or pass --dsn)")
    return create_engine(dsn)


def _groups(engine: Engine, read_only: bool = False) -> dict[str, list[str]]:
    """Map each bound file's source_path to the sorted source_paths of its whole dedup group."""
    with engine.connect() as conn:
        if read_only:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text(_GROUPS_SQL)).all()
    by_root: dict[int, list[str]] = defaultdict(list)
    for path, root in rows:
        by_root[root].append(path)
    return {p: sorted(members) for members in by_root.values() for p in members}


def snapshot(args: argparse.Namespace) -> None:
    """Write the live DB's file -> group map to `args.out` (read-only transaction)."""
    groups = _groups(_engine(args.dsn), read_only=True)
    Path(args.out).write_text(json.dumps(groups, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{len(groups)} bound files, {len({tuple(g) for g in groups.values()})} groups -> {args.out}")


def _score_pairs(pairs: Sequence[tuple[list[int], list[int]]]) -> list[float]:
    """Similarity of each fingerprint pair (a pool worker's chunk)."""
    return [similarity(a, b) for a, b in pairs]


def _chunks(items: Sequence, n: int) -> Iterable[Sequence]:
    """Consecutive slices of `items`, `n` long."""
    return (items[i : i + n] for i in range(0, len(items), n))


def _quantiles(xs: list[float]) -> str:
    """min / p1 / p5 / p25 / median / p75 / p95 / p99 / max of `xs`, compactly."""
    if not xs:
        return "(none)"
    s = sorted(xs)

    def q(p: float) -> float:
        """The p-quantile by nearest rank."""
        return s[min(len(s) - 1, max(0, round(p * (len(s) - 1))))]

    marks = [("min", 0), ("p1", 0.01), ("p5", 0.05), ("p25", 0.25), ("med", 0.5)]
    marks += [("p75", 0.75), ("p95", 0.95), ("p99", 0.99), ("max", 1)]
    return f"n={len(s)} " + " ".join(f"{k}={q(p):.3f}" for k, p in marks)


def _histogram(xs: list[float], lo: float = 0.5, step: float = 0.05) -> str:
    """Counts per `step`-wide bin from `lo` to 1.0 (everything below `lo` in the first bin)."""
    bins: Counter[float] = Counter()
    for x in xs:
        bins[max(lo, min(1.0 - step, step * int(x / step)))] += 1
    return "  ".join(f"{b:.2f}:{bins[b]}" for b in sorted(bins))


def calibrate(args: argparse.Namespace) -> None:
    """Print the same-id vs different-id similarity distributions of fingerprint pairs."""
    with _engine(args.dsn).connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, recording_mbid, fingerprint, fp_duration, source_path FROM trackfile "
                "WHERE fingerprint IS NOT NULL AND recording_mbid IS NOT NULL AND mbid_source = 'MBID'"
            )
        ).all()
    fps = {r.id: r.fingerprint for r in rows}
    rid = {r.id: r.recording_mbid for r in rows}
    dur = {r.id: r.fp_duration for r in rows}
    path = {r.id: r.source_path for r in rows}

    # Same id: every pair sharing a tag recording id, proposed by the index or not.
    by_rid: dict[str, list[int]] = defaultdict(list)
    for fid, r in rid.items():
        by_rid[r].append(fid)
    same = [(a, b) for ids in by_rid.values() for a, b in combinations(sorted(ids), 2)]

    # Different ids: only what the index proposes — the pairs the ladder would ever score.
    index = FingerprintIndex()
    for fid, fp in fps.items():
        index.add(fid, fp)
    proposed: set[tuple[int, int]] = set()
    for fid, fp in fps.items():
        proposed.update((min(fid, o), max(fid, o)) for o in index.candidates(fp) if o != fid)
    diff = sorted(p for p in proposed if rid[p[0]] != rid[p[1]])
    print(f"{len(rows)} files with a tag id and a fingerprint; {len(by_rid)} distinct ids")
    print(f"same-id pairs: {len(same)} ({sum(p in proposed for p in same)} proposed by the index)")
    print(f"different-id pairs proposed by the index: {len(diff)}")

    pairs = same + diff
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        chunks = list(_chunks([(fps[a], fps[b]) for a, b in pairs], 200))
        scores = [x for part in pool.map(_score_pairs, chunks) for x in part]
    score = dict(zip(pairs, scores, strict=True))
    same_s = [score[p] for p in same]
    diff_s = [score[p] for p in diff]
    print(f"same-id:      {_quantiles(same_s)}")
    print(f"              {_histogram(same_s)}")
    print(f"different-id: {_quantiles(diff_s)}")
    print(f"              {_histogram(diff_s)}")
    for t in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9):
        miss = sum(x < t for x in same_s)
        false = sum(x >= t for x in diff_s)
        print(f"  threshold {t:.2f}: same-id below {miss}, different-id at/above {false}")
    print(f"current FP_MATCH = {FP_MATCH}")

    # The top different-id pairs: usually duplicate MusicBrainz recordings of one master.
    print("highest different-id pairs:")
    for a, b in sorted(diff, key=lambda p: score[p], reverse=True)[: args.examples]:
        gap = abs((dur[a] or 0) - (dur[b] or 0))
        print(f"  {score[(a, b)]:.3f} gap={gap:.1f}s  {path[a]}\n                       {path[b]}")
    print("lowest same-id pairs:")
    for a, b in sorted(same, key=lambda p: score[p])[: args.examples]:
        gap = abs((dur[a] or 0) - (dur[b] or 0))
        print(f"  {score[(a, b)]:.3f} gap={gap:.1f}s  {path[a]}\n                       {path[b]}")
    if same_s:
        print(f"same-id mean {statistics.fmean(same_s):.3f}")


def _partial_stored_groups(engine: Engine) -> tuple[int, int, int]:
    """(complete, partial, absent) stored dedup groups, by member hashes present among tracks."""
    with Session(engine) as s:
        # duplicate_hash is a Python property, so it is computed from the loaded tracks.
        present = {t.duplicate_hash for t in s.query(Track)}
        members: dict[int, list[str]] = defaultdict(list)
        for m in s.query(DedupGroupMember):
            members[m.group_id].append(m.member_hash)
    complete = partial = absent = 0
    for hashes in members.values():
        hits = sum(h in present for h in hashes)
        if hits == len(hashes):
            complete += 1
        elif hits:
            partial += 1
        else:
            absent += 1
    return complete, partial, absent


def _distribution(conn: Connection, column: str, table: str, where: str = "TRUE") -> str:
    """`method: count` over one binding column, NULL shown as `-`."""
    rows = conn.execute(
        text(f"SELECT {column}, COUNT(*) FROM {table} WHERE {where} GROUP BY 1 ORDER BY 2 DESC")
    ).all()
    return ", ".join(f"{m or '-'}: {n}" for m, n in rows)


def compare(args: argparse.Namespace) -> None:
    """Compare a rebuilt DB against a snapshot; exit 1 on unbound, missing, extra or lost files."""
    engine = _engine(args.dsn)
    old: dict[str, list[str]] = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    new = _groups(engine)
    with engine.connect() as conn:
        files = conn.execute(
            text(
                "SELECT f.source_path, f.track_id, f.bind_method, f.bind_confidence, "
                "t.canon_id, t.canon_method, COALESCE(t.canon_id, t.id) AS root "
                "FROM trackfile f LEFT JOIN track t ON t.id = f.track_id"
            )
        ).all()
        dist = {
            "file -> track (TrackFile.bind_method)": _distribution(conn, "bind_method", "trackfile"),
            "alias -> track (TrackAlias.match_method)": _distribution(conn, "match_method", "trackalias"),
            "twin -> canon (Track.canon_method)": _distribution(
                conn, "canon_method", "track", "canon_id IS NOT NULL"
            ),
        }
    info = {r.source_path: r for r in files}
    unbound = sorted(p for p, r in info.items() if r.track_id is None)
    missing = sorted(set(old) - set(info))
    extra = sorted(set(info) - set(old))
    print(f"files: {len(info)} in DB, {len(old)} in snapshot; bound {len(info) - len(unbound)}")
    print(f"  missing (in snapshot, not in DB): {len(missing)}; extra (in DB, not in snapshot): {len(extra)}")
    for p in missing[:20] + extra[:20]:
        print(f"    {p}")

    lost = []
    for p, old_group in old.items():
        if p not in new:
            continue  # unbound or missing: reported above
        gone = set(old_group) - set(new[p])
        if gone:
            lost.append((p, old_group, new[p]))
    print(f"files whose group lost members: {len(lost)}")
    for p, og, ng in lost[: args.examples]:
        r = info[p]
        print(f"  LOST {p}  (file {r.bind_method}, twin link {r.canon_method})")
        print(f"    old: {og}\n    new: {ng}")

    # A group grew when its members were in more than one snapshot group (or a newcomer file joined).
    grown: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for members in {tuple(g) for g in new.values()}:
        parts = {tuple(old[p]) if p in old else (p,) for p in members}
        if len(parts) > 1:
            grown[members] = sorted(parts)
    print(f"groups that grew: {len(grown)}")
    by_rung: Counter[str] = Counter()
    for members, parts in sorted(grown.items()):
        # The rung: how each non-canon part is tied in — its tracks' twin link, else its files' binding.
        rungs = sorted({f"{info[p].canon_method or '-'}/{info[p].bind_method or '-'}" for p in members})
        twin_rungs = {info[p].canon_method for p in members if info[p].canon_method}
        # Members' links also carry the group's older manual/flag-set ties; the recording rungs
        # (the only ones new to this pipeline) are what grew it when present.
        recording = twin_rungs & _RECORDING_RUNGS
        by_rung["+".join(sorted(recording or twin_rungs)) or "file binding only"] += 1
        if args.verbose:
            print(f"  grew from {len(parts)} snapshot groups, (twin link/file bind) {rungs}")
            for part in parts:
                print(f"    {list(part)}")
    print(f"grew by twin-link rung: {dict(by_rung)}")

    print("method distribution:")
    for kind, d in dist.items():
        print(f"  {kind}: {d}")
    complete, partial, absent = _partial_stored_groups(engine)
    print(f"stored dedup groups: {complete} complete, {partial} partial, {absent} absent")
    print(f"verdict: unbound {len(unbound)}, missing {len(missing)}, extra {len(extra)}, lost {len(lost)}")
    # The file sets must match exactly: a file alone in its snapshot group loses nothing when it is
    # absent, so `lost` alone would let a dropped file pass.
    if unbound or missing or extra or lost:
        sys.exit(1)


def main() -> None:
    """Parse the subcommand and run it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="file -> dedup group map from a (live) DB, read-only")
    p.add_argument("--dsn", help="prefer $ACCEPTANCE_DSN: argv shows in ps")
    p.add_argument("--out", required=True)
    p.set_defaults(func=snapshot)
    p = sub.add_parser("calibrate", help="same-id vs different-id fingerprint similarity")
    p.add_argument("--dsn", help="prefer $ACCEPTANCE_DSN: argv shows in ps")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--examples", type=int, default=10)
    p.set_defaults(func=calibrate)
    p = sub.add_parser("compare", help="rebuilt DB vs snapshot")
    p.add_argument("--dsn", help="prefer $ACCEPTANCE_DSN: argv shows in ps")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--examples", type=int, default=50)
    p.add_argument("-v", "--verbose", action="store_true", help="list every grown group")
    p.set_defaults(func=compare)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
