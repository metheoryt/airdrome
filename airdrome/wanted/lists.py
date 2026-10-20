"""The two `wanted` lists the owner decides from, each row carrying the signals that decision needs.

- `wanted_library` — library groups (an `APPLE_XML` / `SPOTIFY` source) none of whose members has a file,
  oldest first.
- `wanted_scrobbled` — file-less groups with plays, plus unmatched scrobbled aliases collapsed on
  `(artist_norm, title_norm)`, most played first.

Track rows resolve to the canon root (one hop: `canon_id` is terminal) and sum their signals over the whole
group. Each list is one aggregate SQL for track rows (plus one for unmatched rows); verdict filtering,
sorting and keyset paging (`paging.window`) happen in Python over the result, and `similar_with_file`
runs only for the page served.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy import ColumnElement, Select, distinct, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from airdrome.cloud.sources import SourceTrack
from airdrome.enums import BindMethod, Source, Verdict
from airdrome.models import PlaylistTrack, Track, TrackAlias, TrackAliasScrobble, TrackFile, TrackPlay
from airdrome.normalize.norm import normalize_name
from airdrome.wanted.paging import Key, window
from airdrome.wanted.verdicts import alias_verdicts, group_verdicts


LIBRARY_PROVIDERS: frozenset[Source] = frozenset({Source.APPLE_XML, Source.SPOTIFY})
SIMILAR_MIN_SCORE = 0.6


@dataclass(frozen=True)
class WantedRow:
    """One wanted entry: a file-less canon group (`track`) or collapsed unbound aliases (`unmatched`).

    `rejected` is `UnmatchedGroup.rejected` for an unmatched row (any member manually rejected) and
    always False for a track row.
    """

    kind: Literal["track", "unmatched"]
    track_id: int | None
    alias_ids: list[int]
    title: str | None
    artist: str | None
    album: str | None
    plays: int
    first_played: datetime | None
    last_played: datetime | None
    months: int
    playlists: int
    loved: bool
    rating: int | None
    sources: list[str]
    verdict: Verdict | None
    similar_with_file: dict | None
    rejected: bool


@dataclass(frozen=True)
class Page:
    """One page of a list: its rows, the filtered total, and the cursor of the next page (None at the end)."""

    rows: list[WantedRow]
    total: int
    next_cursor: str | None


@dataclass(frozen=True)
class UnmatchedGroup:
    """Unbound aliases with scrobbles sharing one `(artist_norm, title_norm)` key.

    Display fields come from the member alias with the most scrobbles (lowest id on a tie). `rejected`
    is True when any member was manually rejected (`match_method == MANUAL` with no track).
    """

    alias_ids: list[int]
    title: str | None
    artist: str | None
    album: str | None
    title_norm: str
    artist_norm: str
    scrobbles: int
    first_played: datetime | None
    last_played: datetime | None
    months: int
    rejected: bool


def _root(t: type[Track] = Track) -> ColumnElement[int]:
    """The canon root id of a track row (one hop, `canon_id` is terminal)."""
    return func.coalesce(t.canon_id, t.id)


def _months(col: ColumnElement[datetime] | InstrumentedAttribute[datetime]) -> ColumnElement[int]:
    """Count distinct calendar months of `col`, taken in UTC so the session timezone can't shift them."""
    return func.count(distinct(func.date_trunc("month", func.timezone("UTC", col))))


def _filed_roots() -> Select[tuple[int]]:
    """Roots of groups in which some member has a `TrackFile`."""
    return select(_root()).join(TrackFile, TrackFile.track_id == Track.id)


def _norm_query(query: str | None) -> str | None:
    """`query` normalized like the `_norm` columns, or None when it normalizes to nothing."""
    return (normalize_name(query) or None) if query is not None else None


def _matches(q: str, *cols: ColumnElement[str] | InstrumentedAttribute[str]) -> ColumnElement[bool]:
    """Case-insensitive substring match of `q` on any of `cols`; `%` and `_` in `q` are literal."""
    return or_(*(c.icontains(q, autoescape=True) for c in cols))


def _provider(raw: Source | str) -> Source:
    """A `Source` from an aggregated provider, which arrives as the enum, its name or its value."""
    if isinstance(raw, Source):
        return raw
    return Source[raw] if raw in Source.__members__ else Source(raw)


def _track_rows(s: Session, query: str | None, *, library: bool) -> list[tuple[sa.Row, WantedRow]]:
    """File-less groups that are in the library (`library`) or were played, with their group signals.

    `query` is a raw substring, normalized here like in `unmatched_groups`.
    """
    root = _root()
    # Each signal is aggregated per root in its own CTE before the join: joining plays, playlist entries
    # and sources in one GROUP BY would multiply every count by the other tables' row counts.
    plays = (
        select(
            root.label("root"),
            func.count().label("plays"),
            func.min(TrackPlay.played_at).label("first"),
            func.max(TrackPlay.played_at).label("last"),
            _months(TrackPlay.played_at).label("months"),
        )
        .join(Track, Track.id == TrackPlay.track_id)
        .group_by(root)
        .cte("plays")
    )
    playlists = (
        select(root.label("root"), func.count(distinct(PlaylistTrack.playlist_id)).label("n"))
        .join(Track, Track.id == PlaylistTrack.track_id)
        .group_by(root)
        .cte("playlists")
    )
    sources = (
        select(
            root.label("root"),
            func.array_agg(distinct(SourceTrack.provider)).label("providers"),
            func.bool_or(SourceTrack.provider.in_(LIBRARY_PROVIDERS)).label("library"),
        )
        .join(Track, Track.id == SourceTrack.track_id)
        .group_by(root)
        .cte("sources")
    )
    # Same semantics as `TrackGroup`: loved if any member is, rating = highest non-zero member rating.
    attrs = (
        select(
            root.label("root"),
            func.bool_or(Track.loved).label("loved"),
            func.max(func.nullif(Track.rating, 0)).label("rating"),
        )
        .group_by(root)
        .cte("attrs")
    )
    r = aliased(Track)
    stmt = (
        select(
            r.id,
            r.title,
            r.artist,
            r.album,
            r.title_norm,
            r.artist_norm,
            r.date_added,
            plays.c.plays,
            plays.c.first,
            plays.c.last,
            plays.c.months,
            playlists.c.n,
            sources.c.providers,
            attrs.c.loved,
            attrs.c.rating,
        )
        .select_from(r)
        .join(attrs, attrs.c.root == r.id)
        .outerjoin(plays, plays.c.root == r.id)
        .outerjoin(playlists, playlists.c.root == r.id)
        .outerjoin(sources, sources.c.root == r.id)
        .where(r.canon_id.is_(None), r.id.not_in(_filed_roots()))
        .where(sources.c.library.is_(True) if library else plays.c.plays > 0)
    )
    if q := _norm_query(query):
        stmt = stmt.where(_matches(q, r.title_norm, r.artist_norm))
    verdicts = group_verdicts(s)
    rows: list[tuple[sa.Row, WantedRow]] = []
    for row in s.execute(stmt).all():
        rows.append(
            (
                row,
                WantedRow(
                    kind="track",
                    track_id=row.id,
                    alias_ids=[],
                    title=row.title,
                    artist=row.artist,
                    album=row.album,
                    plays=row.plays or 0,
                    first_played=row.first,
                    last_played=row.last,
                    months=row.months or 0,
                    playlists=row.n or 0,
                    loved=bool(row.loved),
                    rating=row.rating,
                    sources=sorted(_provider(p).value for p in row.providers or []),
                    verdict=verdicts.get(row.id),
                    similar_with_file=None,
                    rejected=False,
                ),
            )
        )
    return rows


def unmatched_groups(s: Session, query: str | None = None) -> list[UnmatchedGroup]:
    """Unbound aliases (rejected included) with ≥ 1 scrobble, collapsed on `(artist_norm, title_norm)`.

    `query` is a raw substring, normalized here and matched on the key norms. Sorted by scrobbles desc,
    then by key.
    """
    # One row per scrobble of an unbound alias, tagged with its alias's scrobble count so the outer
    # aggregate can pick display fields from the most-scrobbled alias without a second pass.
    per = select(
        TrackAlias.id,
        TrackAlias.title,
        TrackAlias.artist,
        TrackAlias.album,
        TrackAlias.title_norm,
        TrackAlias.artist_norm,
        TrackAlias.match_method,
        TrackAliasScrobble.date,
        func.count().over(partition_by=TrackAlias.id).label("n"),
    ).join(TrackAliasScrobble, TrackAliasScrobble.alias_id == TrackAlias.id)
    per = per.where(TrackAlias.track_id.is_(None))
    if q := _norm_query(query):
        per = per.where(_matches(q, TrackAlias.title_norm, TrackAlias.artist_norm))
    p = per.subquery()

    def top(col: ColumnElement[str | None]) -> ColumnElement[str | None]:
        """`col` of the alias with the most scrobbles in the group, lowest id on a tie."""
        return func.array_agg(aggregate_order_by(col, p.c.n.desc(), p.c.id), type_=ARRAY(sa.String))[1]

    stmt = (
        select(
            p.c.title_norm,
            p.c.artist_norm,
            func.array_agg(distinct(p.c.id)).label("alias_ids"),
            top(p.c.title).label("title"),
            top(p.c.artist).label("artist"),
            top(p.c.album).label("album"),
            func.count().label("scrobbles"),
            func.min(p.c.date).label("first"),
            func.max(p.c.date).label("last"),
            _months(p.c.date).label("months"),
            func.bool_or(p.c.match_method == BindMethod.MANUAL).label("rejected"),
        )
        # A blank artist is just the key `""`, so it never collapses into a named artist's row.
        .group_by(p.c.artist_norm, p.c.title_norm)
        .order_by(func.count().desc(), p.c.title_norm, p.c.artist_norm)
    )
    return [
        UnmatchedGroup(
            alias_ids=sorted(row.alias_ids),
            title=row.title,
            artist=row.artist,
            album=row.album,
            title_norm=row.title_norm,
            artist_norm=row.artist_norm,
            scrobbles=row.scrobbles,
            first_played=row.first,
            last_played=row.last,
            months=row.months,
            rejected=bool(row.rejected),
        )
        for row in s.execute(stmt).all()
    ]


def fold_alias_verdict(verdicts: dict[int, Verdict], alias_ids: list[int]) -> Verdict | None:
    """The effective verdict over `alias_ids` from an `alias_verdicts` map, `SKIP` winning."""
    found = {verdicts[a] for a in alias_ids if a in verdicts}
    if Verdict.SKIP in found:
        return Verdict.SKIP
    return Verdict.WANTED if found else None


def similar_with_file(s: Session, title_norm: str, artist_norm: str, exclude_root: int | None) -> dict | None:
    """The filed group most like `(title_norm, artist_norm)` outside `exclude_root`'s group, if ≥ 0.6.

    Reported as the group's root (id, title, artist), whichever member scored best.
    """
    title_sim = func.similarity(Track.title_norm, title_norm)
    artist_sim = func.similarity(Track.artist_norm, artist_norm)
    score = (title_sim * 0.5 + artist_sim * 0.5).label("score")
    root = _root()
    member = aliased(Track)
    top = aliased(Track)
    # Correlated per candidate (few, after the prefilter): a file on the root itself or on any twin of it.
    # Both lookups hit an index; an `IN (filed roots)` semi-join instead hashes every file on every call.
    has_file = or_(
        select(TrackFile.id).where(TrackFile.track_id == root).exists(),
        select(TrackFile.id)
        .join(member, member.id == TrackFile.track_id)
        .where(member.canon_id == root)
        .exists(),
    )
    stmt = (
        select(top.id, top.title, top.artist, score)
        .select_from(Track)
        .join(top, top.id == root)
        # Lossless prefilter that lets the GIN trigram indexes do the work: a score ≥ 0.6 needs one of
        # the two similarities ≥ 0.6, above pg_trgm's default `%` threshold of 0.3. The artist index is
        # partial (`artist_norm <> ''`), so the predicate repeats that condition or the index is unusable;
        # a blank artist scores 0 anyway, so nothing is lost.
        .where(
            or_(
                Track.title_norm.op("%")(title_norm),
                sa.and_(Track.artist_norm != "", Track.artist_norm.op("%")(artist_norm)),
            )
        )
        .where(has_file)
        .order_by(score.desc(), Track.id)
        .limit(1)
    )
    if exclude_root is not None:
        stmt = stmt.where(root != exclude_root)
    row = s.execute(stmt).first()
    if row is None or row.score < SIMILAR_MIN_SCORE:
        return None
    return {"track_id": row.id, "title": row.title, "artist": row.artist, "score": round(float(row.score), 3)}


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(at: datetime) -> int:
    """`at` as integer microseconds since the epoch: exact, and sorts like `at` (ISO text does not)."""
    return (at - _EPOCH) // timedelta(microseconds=1)


def _keep(verdict: Verdict | None, wanted: Verdict | None) -> bool:
    """Whether a row with `verdict` passes the filter: exact when `wanted` is set, else anything but SKIP."""
    return verdict == wanted if wanted is not None else verdict != Verdict.SKIP


type _Entry = tuple[str, str, int | None, WantedRow]


def _page(
    s: Session, name: str, entries: list[_Entry], key: Callable[[_Entry], Key], cursor: str | None, limit: int
) -> Page:
    """Serve the window of sorted `(title_norm, artist_norm, exclude_root, row)` entries after `cursor`.

    `similar_with_file` is attached here, for the served rows only.
    """
    w = window(name, entries, key, cursor, limit)
    out = [
        _with_similar(row, similar_with_file(s, title_norm, artist_norm, exclude_root))
        for title_norm, artist_norm, exclude_root, row in w.items
    ]
    return Page(rows=out, total=w.total, next_cursor=w.next_cursor)


def _with_similar(row: WantedRow, similar: dict | None) -> WantedRow:
    """`row` with its `similar_with_file` filled in."""
    return replace(row, similar_with_file=similar)


def wanted_library(
    s: Session,
    cursor: str | None = None,
    limit: int = 50,
    verdict: Verdict | None = None,
    query: str | None = None,
) -> Page:
    """Library groups with no file on any member, oldest `date_added` first."""
    tracks = [(t, r) for t, r in _track_rows(s, query, library=True) if _keep(r.verdict, verdict)]
    added = {t.id: _micros(t.date_added) for t, _ in tracks}
    entries = [(t.title_norm, t.artist_norm, t.id, r) for t, r in tracks]
    # The root id (entry[2]) breaks `date_added` ties, so keys are unique, as keyset paging requires.
    key: Callable[[_Entry], Key] = lambda e: (added[e[2]], e[2])  # noqa: E731
    entries.sort(key=key)
    return _page(s, "wanted_library", entries, key, cursor, limit)


def wanted_scrobbled(
    s: Session,
    cursor: str | None = None,
    limit: int = 50,
    verdict: Verdict | None = None,
    query: str | None = None,
    include_unmatched: bool = True,
) -> Page:
    """File-less played groups and (optionally) unmatched scrobbled aliases, most plays first."""
    entries: list[tuple[str, str, int | None, WantedRow]] = [
        (t.title_norm, t.artist_norm, t.id, r)
        for t, r in _track_rows(s, query, library=False)
        if _keep(r.verdict, verdict)
    ]
    if include_unmatched:
        verdicts = alias_verdicts(s)
        for g in unmatched_groups(s, query):
            row = WantedRow(
                kind="unmatched",
                track_id=None,
                alias_ids=g.alias_ids,
                title=g.title,
                artist=g.artist,
                album=g.album,
                plays=g.scrobbles,
                first_played=g.first_played,
                last_played=g.last_played,
                months=g.months,
                playlists=0,
                loved=False,
                rating=None,
                sources=[],
                verdict=fold_alias_verdict(verdicts, g.alias_ids),
                similar_with_file=None,
                rejected=g.rejected,
            )
            if _keep(row.verdict, verdict):
                entries.append((g.title_norm, g.artist_norm, None, row))
    entries.sort(key=_scrobbled_key)
    return _page(s, "wanted_scrobbled", entries, _scrobbled_key, cursor, limit)


def _scrobbled_key(e: _Entry) -> Key:
    """Most plays first; ties broken by title, kind and id into a total order, so keys are unique."""
    row = e[3]
    return (-row.plays, (row.title or "").casefold(), row.kind, row.track_id or 0, (row.alias_ids or [0])[0])
