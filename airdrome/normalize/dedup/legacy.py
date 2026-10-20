"""Reading of the old hash-multiset dedup groups and their conversion to pair decisions.

The old file is `{label: {"members": [hash, ...], "canon_hashes": [hash | None, ...]}}`. A hash names
every track whose `duplicate_hash` equals it, so conversion needs the live hash -> key map. The first
half is pure; the DB half (`read_legacy_groups` onward) reads the live group tables through Core, with
the module's own `LEGACY` metadata, so it keeps working once the ORM models are gone. It only reads.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import Column, Connection, ForeignKey, Integer, MetaData, String, Table, select

from airdrome.cloud import sources  # noqa: F401  (registers SourceTrack, which Track's relationships name)
from airdrome.conf import settings
from airdrome.enums import DecisionKind, DecisionOrigin
from airdrome.models import Track

from .decisions import Canon, Outcome, Pair, dump_v2, key_text, pair, require_aware, resolve
from .grouping import canon_order


@dataclass(frozen=True)
class LegacyGroup:
    """One stored group: a label and (member_hash, canon_hash | None) pairs."""

    label: str | None
    members: list[tuple[str, str | None]]


@dataclass
class Converted:
    """Decisions from one group, plus what the conversion dropped or could not pin down."""

    pairs: list[Pair] = field(default_factory=list)
    canons: list[Canon] = field(default_factory=list)
    dropped: int = 0  # distinct member hashes with no live track of their own
    ambiguous: int = 0  # member hashes with more than one live key
    dangling_canon: int = 0  # kept members whose canon hash has no live track
    conflicting_canons: int = 0  # member hashes whose occurrences name more than one canon hash


def legacy_groups(data: dict) -> list[LegacyGroup]:
    """Read the old file shape into groups, in file order; ValueError on a malformed entry."""
    groups = []
    for label, entry in data.items():
        try:
            members, canons = entry["members"], entry["canon_hashes"]
            groups.append(LegacyGroup(label, list(zip(members, canons, strict=True))))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed legacy group {label!r}: {exc!r}") from exc
    return groups


def legacy_hash(
    title: str,
    artist: str | None,
    album_artist: str | None,
    album: str | None,
    track_n: int | None,
    disc_n: int | None,
    year: int | None,
    duration: int | None,
) -> str:
    """Return the old `Track.duplicate_hash` of a track's fields (the property itself is gone).

    The frozen format the old groups and mirror were written in: title, artist, album artist, album,
    track and disc number, year and the duration rounded to 5 s, `;`-joined with None as "".
    `test_legacy_hash_format` pins it with literal values.
    """
    duration_bucket = round(duration / 5) * 5 if duration is not None else None
    fields = (title, artist, album_artist, album, track_n, disc_n, year, duration_bucket)
    return ";".join(str(v) if v is not None else "" for v in fields)


def convert_group(
    group: LegacyGroup,
    keys_of_hash: Mapping[str, list[str]],
    pick: Callable[[list[str]], str],
    at: datetime,
    origin: DecisionOrigin,
) -> Converted:
    """Convert one stored group to decisions; `keys_of_hash` lists live key texts in track-id order.

    Occurrences are grouped by member hash first (two tracks can share a hash, and the live writer stores
    one member per track), so the result is the same under any member order. A hash resolves to the set of
    canon hashes named for it, or to itself when it names none; two hashes are `different` only when those
    sets do not overlap.
    """
    require_aware(at)
    out = Converted()
    pairs: dict[tuple[str, str], Pair] = {}

    def add(x: str, y: str, kind: DecisionKind) -> None:
        """Record a decision between two keys, skipping a key with itself."""
        if x != y:
            p = pair(x, y, kind, at, origin)
            pairs.setdefault((p.a, p.b), p)

    named: dict[str, set[str]] = {}
    for member_hash, canon_hash in group.members:
        named.setdefault(member_hash, set())
        if canon_hash is not None:
            named[member_hash].add(canon_hash)

    resolved: dict[str, frozenset[str]] = {}
    for h in sorted(named):
        keys = keys_of_hash.get(h, [])
        if not keys:
            out.dropped += 1
            continue
        if len(keys) > 1:
            out.ambiguous += 1
        if len(named[h]) > 1:
            out.conflicting_canons += 1
        resolved[h] = frozenset(named[h]) or frozenset((h,))
        if any(not keys_of_hash.get(c) for c in named[h]):
            out.dangling_canon += 1  # kept: it still vetoes the members that resolve elsewhere
        for c in sorted(named[h]):
            canon_keys = keys_of_hash.get(c, [])
            if canon_keys:
                for key in keys:
                    add(key, canon_keys[0], DecisionKind.SAME)

    live = sorted(resolved)
    for i, h1 in enumerate(live):
        for h2 in live[i + 1 :]:
            if resolved[h1].isdisjoint(resolved[h2]):
                for k1 in keys_of_hash[h1]:
                    for k2 in keys_of_hash[h2]:
                        add(k1, k2, DecisionKind.DIFFERENT)

    # A canon is recorded only where a person picked one: not for MCP groups, and only when the
    # stored canon differs from what canon_order would choose for its sub-group anyway.
    if group.label is None or not group.label.startswith("mcp:"):
        chosen = set()
        for c in sorted(set().union(*named.values())):
            canon_keys = keys_of_hash.get(c, [])
            if not canon_keys:
                continue
            sub = list(canon_keys)
            for h in live:
                if h != c and c in resolved[h]:
                    sub.extend(keys_of_hash[h])
            if pick(sub) != canon_keys[0]:
                chosen.add(canon_keys[0])
        out.canons = [Canon(t, at, origin) for t in sorted(chosen)]

    out.pairs = sorted(pairs.values(), key=lambda p: (p.a, p.b))
    return out


# --- DB half ----------------------------------------------------------------------------------------

LEGACY = MetaData()  # the module's own; never `AirdromeBase.metadata`

_GROUP = Table(
    "dedupgroup", LEGACY, Column("id", Integer, primary_key=True), Column("label", String, nullable=True)
)
_MEMBER = Table(
    "dedupgroupmember",
    LEGACY,
    Column("id", Integer, primary_key=True),
    Column("group_id", Integer, ForeignKey("dedupgroup.id", ondelete="CASCADE"), nullable=False),
    Column("member_hash", String, nullable=False),
    Column("canon_hash", String, nullable=True),
)


def read_legacy_groups(conn: Connection) -> list[tuple[int, LegacyGroup]]:
    """Read every stored group as (id, LegacyGroup), in `dedupgroup.id` order, members in row order."""
    members: dict[int, list[tuple[str, str | None]]] = defaultdict(list)
    for gid, member_hash, canon_hash in conn.execute(
        select(_MEMBER.c.group_id, _MEMBER.c.member_hash, _MEMBER.c.canon_hash).order_by(_MEMBER.c.id)
    ):
        members[gid].append((member_hash, canon_hash))
    return [
        (gid, LegacyGroup(label, members[gid]))
        for gid, label in conn.execute(select(_GROUP.c.id, _GROUP.c.label).order_by(_GROUP.c.id))
    ]


@dataclass
class LiveTracks:
    """The live tracks as the migration sees them."""

    id_of_key: dict[str, int]  # key text -> track id (the lowest id when a key is shared)
    keys_of_hash: dict[str, list[str]]  # legacy hash -> key texts, in track-id order
    name_of_key: dict[str, str]  # key text -> "artist — title [album]"
    shared_keys: int = 0  # keys carried by more than one track (the live constraint treats NULLs as distinct)


def live_tracks(conn: Connection) -> LiveTracks:
    """Read the live tracks' key and hash fields (explicit columns: the live schema may differ elsewhere)."""
    c = Track.__table__.c
    rows = conn.execute(
        select(
            c.id, c.title, c.artist, c.album, c.album_artist, c.track_n, c.disc_n, c.year, c.duration
        ).order_by(c.id)
    )
    live = LiveTracks({}, defaultdict(list), {})
    seen: Counter[str] = Counter()
    for tid, title, artist, album, album_artist, track_n, disc_n, year, duration in rows:
        key = key_text((title, artist, album, album_artist))
        seen[key] += 1
        live.id_of_key.setdefault(key, tid)
        live.name_of_key.setdefault(key, f"{artist or ''} — {title} [{album or ''}]")
        h = legacy_hash(title, artist, album_artist, album, track_n, disc_n, year, duration)
        live.keys_of_hash[h].append(key)
    live.shared_keys = sum(1 for n in seen.values() if n > 1)
    live.keys_of_hash = dict(live.keys_of_hash)  # no accidental insertion on a miss
    return live


def canon_pick(conn: Connection, live: LiveTracks) -> Callable[[list[str]], str]:
    """Return `pick(keys) -> key` choosing what `canon_order` would make canon among the given keys."""
    key_of_id = {i: k for k, i in live.id_of_key.items()}

    def pick(keys: list[str]) -> str:
        """Return the key of the track `canon_order` ranks first among `keys`."""
        ids = [live.id_of_key[k] for k in keys]
        best = conn.execute(
            select(Track.id).where(Track.id.in_(ids)).order_by(*canon_order()).limit(1)
        ).scalar_one()
        return key_of_id[best]

    return pick


@dataclass
class Cluster:
    """Groups that share a member hash, with their kind and a human-readable account."""

    group_ids: list[int]
    kind: str  # "clean", "canon-choice" or "contradiction"
    text: str


@dataclass
class Migration:
    """The converted decisions plus everything the owner must review before trusting them."""

    pairs: list[Pair]
    canons: list[Canon]
    groups: int
    dropped: int
    ambiguous: int
    shared_keys: int
    clusters: list[Cluster]
    dangling_canon: int = 0
    conflicting_canons: int = 0


def _clusters_of(groups: list[tuple[int, LegacyGroup]]) -> list[list[int]]:
    """Return connected components (as indexes into `groups`) of >= 2 groups sharing a member hash."""
    parent = list(range(len(groups)))

    def find(x: int) -> int:
        """Return the root of `x` with path halving."""
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_with: dict[str, int] = {}
    for i, (_, g) in enumerate(groups):
        for h in {h for pair_ in g.members for h in pair_ if h is not None}:
            parent[find(i)] = find(first_with.setdefault(h, i))
    by_root: dict[int, list[int]] = defaultdict(list)
    for i in range(len(groups)):
        by_root[find(i)].append(i)
    return sorted((m for m in by_root.values() if len(m) >= 2), key=min)


def migrate(conn: Connection, now: datetime) -> Migration:
    """Convert every stored group; a later group's row wins a repeated pair or canon key."""
    require_aware(now)
    groups = read_legacy_groups(conn)
    live = live_tracks(conn)
    pick = canon_pick(conn, live)
    n = len(groups)
    converted = [
        convert_group(g, live.keys_of_hash, pick, now - timedelta(seconds=n - i), DecisionOrigin.MIGRATED)
        for i, (_, g) in enumerate(groups)
    ]
    pairs: dict[tuple[str, str], Pair] = {}
    canons: dict[str, Canon] = {}
    for conv in converted:  # id order: the later group overwrites
        pairs.update({(p.a, p.b): p for p in conv.pairs})
        canons.update({c.track: c for c in conv.canons})
    clusters = [_describe(groups, converted, idx, live) for idx in _clusters_of(groups)]
    return Migration(
        pairs=sorted(pairs.values(), key=lambda p: (p.a, p.b)),
        canons=sorted(canons.values(), key=lambda c: c.track),
        groups=n,
        dropped=sum(c.dropped for c in converted),
        ambiguous=sum(c.ambiguous for c in converted),
        shared_keys=live.shared_keys,
        clusters=clusters,
        dangling_canon=sum(c.dangling_canon for c in converted),
        conflicting_canons=sum(c.conflicting_canons for c in converted),
    )


def _describe(
    groups: list[tuple[int, LegacyGroup]], converted: list[Converted], idx: list[int], live: LiveTracks
) -> Cluster:
    """Classify one cluster (groups at `idx`) and write out its groups, resolution and superseded rows."""
    name_of_id = {live.id_of_key[k]: v for k, v in live.name_of_key.items()}

    def name(h: str) -> str:
        """Name the tracks a member hash stands for."""
        keys = live.keys_of_hash.get(h)
        return " | ".join(live.name_of_key[k] for k in keys) if keys else f"(no live track) {h}"

    latest: dict[tuple[str, str], Pair] = {}
    owner: dict[tuple[str, str], int] = {}  # group id that wrote the pair now in `latest`
    replaced: list[tuple[int, Pair]] = []
    kinds: dict[tuple[str, str], set[DecisionKind]] = defaultdict(set)
    canons: dict[str, Canon] = {}
    for i in idx:
        for p in converted[i].pairs:
            kinds[(p.a, p.b)].add(p.kind)
            if (p.a, p.b) in latest:
                replaced.append((owner[(p.a, p.b)], latest[(p.a, p.b)]))
            latest[(p.a, p.b)] = p
            owner[(p.a, p.b)] = groups[i][0]
        canons.update({c.track: c for c in converted[i].canons})
    res = resolve(latest.values(), canons.values(), live.id_of_key)

    named = {c for i in idx for _, c in groups[i][1].members if c is not None and live.keys_of_hash.get(c)}
    comp_of = {t: comp for comp in res.components for t in comp}
    canon_comps = {comp_of.get(live.id_of_key[live.keys_of_hash[c][0]]) for c in named} - {None}
    # ≥ 2 distinct canon hashes whose tracks resolve into one component
    clashing = any(
        len({c for c in named if comp_of.get(live.id_of_key[live.keys_of_hash[c][0]]) == comp}) >= 2
        for comp in canon_comps
    )
    if any(len(v) == 2 for v in kinds.values()):
        kind = "contradiction"
    elif clashing:
        kind = "canon-choice"
    else:
        kind = "clean"

    lines = []
    for i in idx:
        gid, g = groups[i]
        lines.append(f"  group {gid} ({g.label}):")
        lines += [f"    {name(h)} -> {name(c) if c is not None else 'root'}" for h, c in g.members]
    lines.append("  resolves to:")
    for comp in res.components:
        lines.append("    component: " + "; ".join(sorted(name_of_id[t] for t in comp)))
        picked = [t for t in comp if t in res.canons]
        canon = name_of_id[max(picked, key=res.canons.get)] if picked else "canon_order at dedup time"
        lines.append(f"      canon: {canon}")
    for pair_ids in sorted(tuple(sorted(s)) for s in res.cannot_link):
        lines.append("    separated: " + " / ".join(name_of_id[t] for t in pair_ids))
    sup = [ab for ab, o in res.outcomes.items() if o is Outcome.SUPERSEDED]
    sup_lines = [
        f"    group {gid}: {p.kind.value} {live.name_of_key.get(p.a, p.a)} / {live.name_of_key.get(p.b, p.b)}"
        f" (replaced by a later group)"
        for gid, p in replaced
    ] + [
        f"    {latest[ab].kind.value} {live.name_of_key.get(ab[0], ab[0])} / "
        f"{live.name_of_key.get(ab[1], ab[1])}"
        f" (superseded by a newer decision)"
        for ab in sup
    ]
    lines.append("  superseded decisions:" + ("" if sup_lines else " none"))
    lines += sup_lines
    return Cluster([groups[i][0] for i in idx], kind, "\n".join(lines))


def format_report(m: Migration) -> str:
    """Render every cluster and the closing totals as text."""
    out = []
    for c in m.clusters:
        out.append(f"cluster of groups {c.group_ids}: {c.kind}")
        out.append(c.text)
    same = sum(p.kind is DecisionKind.SAME for p in m.pairs)
    kinds = Counter(c.kind for c in m.clusters)
    out += [
        f"groups read: {m.groups}",
        f"pairs: same {same}, different {len(m.pairs) - same}; canons {len(m.canons)}",
        f"dropped {m.dropped}, ambiguous {m.ambiguous}, shared keys {m.shared_keys}, "
        f"dangling canon {m.dangling_canon}, conflicting canons {m.conflicting_canons}",
        "clusters: "
        + ", ".join(f"{k} {kinds.get(k, 0)}" for k in ("clean", "canon-choice", "contradiction")),
    ]
    return "\n".join(out) + "\n"


def check_out(out: Path) -> None:
    """Raise ValueError when `out` exists or is the live mirror path."""
    if out.exists():
        raise ValueError(f"{out} already exists")
    if out.resolve() == settings.duplicates_file.resolve():
        raise ValueError(f"{out} is the live mirror path; write elsewhere and move it by hand")


def write_migration(conn: Connection, out: Path, now: datetime) -> Migration:
    """Migrate and write the v2 document to a fresh `out`."""
    from .mirror import write_mirror  # lazy: mirror imports persistence, which must not import this

    check_out(out)
    m = migrate(conn, now)
    write_mirror(dump_v2(m.pairs, m.canons), out)
    return m
