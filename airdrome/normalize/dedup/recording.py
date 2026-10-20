"""Recording identity as a `dedup` grouping input: a shared recording id, or matching audio.

`dedup` rebuilds every `canon_id` from scratch, so links the identity ladder made at `land` would
be erased by the next run unless `dedup` reproduces them. This module finds them again from the
evidence on the files (docs/design/identity-ladder.md, *Recording-identity groups survive dedup*):

- **Recording id.** Tracks whose files agree on one `recording_mbid` (`recording_of_track`) group.
  A track whose files carry *different* ids is *conflicted*: it joins no recording group (one
  mis-bound file must not chain two groups together) and is reported instead.
- **Fingerprint.** Each track contributes one representative file (its highest-bitrate fingerprinted
  file, then the lowest id; never the main flag, which moves with the group), looked up through
  `FingerprintIndex`, so no pass compares every pair. A match at or above `FP_MATCH` links
  automatically when the durations differ by at most `FP_MAX_DURATION_GAP`; with a larger gap it is
  only a review candidate.

Two different agreed recording ids never end in one group: a fingerprint edge between them is
dropped, and a component never collects a second id (an id-less track matching an X track and a
Y track joins only the first it reaches).
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from airdrome.enums import BindMethod
from airdrome.library.fingerprint import FP_MATCH, FingerprintIndex
from airdrome.library.ladder import FP_MAX_DURATION_GAP, MBID_CONFIDENCE, is_conflicted, recording_of_track
from airdrome.models import Track, TrackFile

from .grouping import CanonStrategy, canon_order


@dataclass
class RecordingScan:
    """Everything one recording pass found; `compute_recording_groups` is its public view."""

    auto: list[tuple[str, list[Track]]] = field(default_factory=list)  # (label, canon-ordered)
    review: list[list[Track]] = field(default_factory=list)
    conflicted: list[Track] = field(default_factory=list)
    # Track id -> the strongest evidence that put it in its auto group (method, confidence).
    evidence: dict[int, tuple[BindMethod, float]] = field(default_factory=dict)


class _Components:
    """Union-find over track ids that keeps each component's set of agreed recording ids."""

    def __init__(self) -> None:
        """Start with no tracks."""
        self.parent: dict[int, int] = {}
        self.ids: dict[int, set[str]] = {}  # root -> recording ids its members agree on

    def add(self, tid: int, rid: str | None) -> None:
        """Register track `tid` (agreeing on `rid`, if any) as its own component."""
        self.parent[tid] = tid
        self.ids[tid] = {rid} if rid else set()

    def find(self, x: int) -> int:
        """Root of `x`'s component, halving the path as it goes."""
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        """Join `a` and `b` unless the result would hold two recording ids; True if they are joined."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if len(self.ids[ra] | self.ids[rb]) > 1:
            return False
        self.parent[ra] = rb
        self.ids[rb] |= self.ids.pop(ra)
        return True

    def groups(self, order: dict[int, int]) -> list[list[int]]:
        """Components of two or more tracks, members and components sorted by `order`."""
        out: dict[int, list[int]] = {}
        for tid in sorted(self.parent, key=order.__getitem__):
            out.setdefault(self.find(tid), []).append(tid)
        return [g for g in out.values() if len(g) >= 2]


def _representative(t: Track) -> TrackFile | None:
    """The file whose fingerprint stands for `t`: its highest-bitrate fingerprinted file, then the lowest id.

    Never `is_main`: the main file moves with the group a track ends in, so reading it would make the next
    run's grouping depend on the last one (and an edit's plan differ from the `dedup` after it).
    """
    usable = [f for f in t.files if f.fingerprint and f.fp_duration is not None]
    return min(usable, key=lambda f: (-(f.bitrate or 0), f.id)) if usable else None


def _id_method(t: Track, rid: str) -> BindMethod:
    """How ``t`` got recording id ``rid``: ACOUSTID if any of its files carrying it got it by lookup."""
    lookup = any(f.recording_mbid == rid and f.mbid_source == BindMethod.ACOUSTID for f in t.files)
    return BindMethod.ACOUSTID if lookup else BindMethod.MBID


def _keep_stronger(
    evidence: dict[int, tuple[BindMethod, float]], tid: int, new: tuple[BindMethod, float]
) -> None:
    """Record ``new`` as ``tid``'s evidence unless what it already has is at least as confident."""
    prev = evidence.get(tid)
    if prev is None or prev[1] < new[1]:
        evidence[tid] = new


def scan_recordings(s: Session, strategy: CanonStrategy = CanonStrategy.ADDED) -> RecordingScan:
    """Find recording-identity groups, review candidates and conflicted tracks; no writes.

    Groups come back in `canon_order(strategy)`, so group[0] is the canon candidate exactly as for
    the flag-set groups they are merged with.
    """
    query = select(Track).options(selectinload(Track.files)).order_by(*canon_order(strategy))
    tracks = list(s.scalars(query))
    order = {t.id: i for i, t in enumerate(tracks)}
    by_id = {t.id: t for t in tracks}
    scan = RecordingScan()

    comps = _Components()
    rid_of: dict[int, str | None] = {}
    for t in tracks:
        if is_conflicted(t):
            scan.conflicted.append(t)
            continue
        rid = recording_of_track(t)
        rid_of[t.id] = rid
        comps.add(t.id, rid)

    # Recording id: every track agreeing on one id joins one component.
    first_with: dict[str, int] = {}
    for tid, rid in rid_of.items():
        if rid is None:
            continue
        if rid in first_with:
            first = first_with[rid]
            comps.union(first, tid)
            # The link is as strong as its weaker end: an id either side got from AcoustID is a lookup's.
            methods = {_id_method(by_id[first], rid), _id_method(by_id[tid], rid)}
            method = BindMethod.ACOUSTID if BindMethod.ACOUSTID in methods else BindMethod.MBID
            link = (method, MBID_CONFIDENCE[method])
            scan.evidence[tid] = link
            # The first track pairs with every later one: keep its strongest link, not its last.
            _keep_stronger(scan.evidence, first, link)
        else:
            first_with[rid] = tid

    # Fingerprint: index one file per track, then look each track up once.
    reps = {tid: f for tid in rid_of if (f := _representative(by_id[tid])) is not None}
    index = FingerprintIndex()
    for tid, f in reps.items():
        index.add(tid, f.fingerprint or [])
    auto_edges: list[tuple[float, int, int]] = []
    review_edges: list[tuple[int, int]] = []
    for tid, f in reps.items():
        for other, score in index.matches(f.fingerprint or [], threshold=FP_MATCH):
            if other <= tid:  # each pair once (and never a track with itself)
                continue
            a, b = rid_of[tid], rid_of[other]
            if a and b and a != b:
                continue  # different recordings by their ids, however alike the audio
            if abs((f.fp_duration or 0.0) - (reps[other].fp_duration or 0.0)) <= FP_MAX_DURATION_GAP:
                auto_edges.append((score, tid, other))
            else:
                review_edges.append((tid, other))

    # Strongest matches first, so a track that could join either of two ids joins the closer one.
    for score, a, b in sorted(auto_edges, key=lambda e: (-e[0], order[e[1]], order[e[2]])):
        if not comps.union(a, b):
            continue
        for tid in (a, b):
            prev = scan.evidence.get(tid)
            if prev is None or (prev[0] is BindMethod.FINGERPRINT and prev[1] < score):
                scan.evidence[tid] = (BindMethod.FINGERPRINT, score)

    for j, ids in enumerate(comps.groups(order)):
        method = "mbid" if all(scan.evidence[tid][0] in MBID_CONFIDENCE for tid in ids) else "fp"
        scan.auto.append((f"rec[{method}]#{j}", [by_id[tid] for tid in ids]))

    # Review: the larger-gap matches between tracks the automatic pass left apart.
    review = _Components()
    for a, b in review_edges:
        if comps.find(a) == comps.find(b):
            continue
        for tid in (a, b):
            if tid not in review.parent:
                review.add(tid, rid_of[tid])
        review.union(a, b)
    scan.review = [[by_id[tid] for tid in ids] for ids in review.groups(order)]
    return scan


def compute_recording_groups(
    s: Session, strategy: CanonStrategy = CanonStrategy.ADDED
) -> tuple[list[list[Track]], list[list[Track]]]:
    """`(auto, review)` recording groups, each in canon order; conflicted tracks are in neither."""
    scan = scan_recordings(s, strategy)
    return [g for _, g in scan.auto], scan.review
