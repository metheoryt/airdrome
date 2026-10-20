"""The track card (everything about one canon group) and the unmatched-alias list with bind candidates.

Both return plain JSON-serializable data (datetimes as ISO strings, enums as their values). Counts come
from SQL, never from relationship collections: an edit earlier in the same session (a bind re-points
`TrackPlay` rows by `track_id`) can leave `Track.plays` and `Track.aliases` stale.
"""

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourceTrack
from airdrome.library.edits import group_of
from airdrome.match import find_matches
from airdrome.models import (
    CanonChoice,
    PairDecision,
    Playlist,
    PlaylistTrack,
    Track,
    TrackAlias,
    TrackAliasScrobble,
    TrackFile,
    TrackPlay,
)
from airdrome.normalize.dedup.decisions import Resolution, key_text, parse_key, track_key
from airdrome.normalize.dedup.store import live_keys, resolve_library
from airdrome.normalize.norm import normalize_name
from airdrome.wanted.lists import UnmatchedGroup, unmatched_groups
from airdrome.wanted.paging import Key, window
from airdrome.wanted.verdicts import group_verdicts


ALIAS_CAP = 50
CANDIDATES = 5
# Looser than `land`'s gates (0.4 / 0.45): an unmatched alias is one `land` already refused, so the
# strict gates would leave nothing to suggest. The owner reviews every candidate by hand.
CAND_THRESHOLD = 0.2
CAND_TITLE_THRESHOLD = 0.3


def _member(t: Track, sources: list[dict], files: list[dict]) -> dict:
    """One group member as card data."""
    return {
        "id": t.id,
        "title": t.title,
        "artist": t.artist,
        "album_artist": t.album_artist,
        "album": t.album,
        "track_n": t.track_n,
        "disc_n": t.disc_n,
        "year": t.year,
        "duration": t.duration,
        "compilation": t.compilation,
        "loved": t.loved,
        "rating": t.rating,
        "canon_method": t.canon_method.value if t.canon_method else None,
        "canon_confidence": t.canon_confidence,
        "sources": sources,
        "files": files,
    }


def _decisions(s: Session, members: list[Track], resolution: Resolution | None) -> dict:
    """The manual decisions touching `members`: every pair with an end among their keys, newest first,
    and their canon choices, each with what the resolution made of it.

    A pair is `{a, b, kind, decided_at, origin, status}`, each end `{key, track_id}` (`track_id` None
    when no live track carries the key); `status` is accepted, superseded or dead. A canon is
    `{key, track_id, decided_at, origin, status}` with status accepted or dead.
    """
    keys = [key_text(track_key(t)) for t in members]
    live = live_keys(s)
    outcomes = (resolution or resolve_library(s)).outcomes

    def end(k: str) -> dict:
        """One named track: its key and its live id."""
        return {"key": list(parse_key(k)), "track_id": live.get(k)}

    rows = s.scalars(
        select(PairDecision).where(or_(PairDecision.key_a.in_(keys), PairDecision.key_b.in_(keys)))
    )
    pairs = sorted(rows, key=lambda r: (-r.decided_at.timestamp(), r.key_a, r.key_b))
    canons = sorted(s.scalars(select(CanonChoice).where(CanonChoice.key.in_(keys))), key=lambda r: r.key)
    return {
        "pairs": [
            {
                "a": end(r.key_a),
                "b": end(r.key_b),
                "kind": r.kind.value,
                "decided_at": r.decided_at.isoformat(),
                "origin": r.origin.value,
                "status": outcomes[(r.key_a, r.key_b)].value,
            }
            for r in pairs
        ],
        "canons": [
            {
                "key": list(parse_key(r.key)),
                "track_id": live.get(r.key),
                "decided_at": r.decided_at.isoformat(),
                "origin": r.origin.value,
                "status": "accepted" if r.key in live else "dead",
            }
            for r in canons
        ],
    }


def track_card(s: Session, track_id: int, resolution: Resolution | None = None) -> dict:
    """Everything about the canon group of `track_id` (any member), or `EditError` on an unknown id.

    `resolution` is the library's current `resolve_library` result when the caller already holds it (two
    cards after one edit); omitted, the card resolves the decisions itself.
    """
    group = group_of(s, track_id)
    ids = [t.id for t in group]
    root = group[0]

    sources: dict[int, list[dict]] = {i: [] for i in ids}
    for st in s.scalars(select(SourceTrack).where(SourceTrack.track_id.in_(ids)).order_by(SourceTrack.id)):
        sources[st.track_id].append({"provider": st.provider.value, "source_id": st.source_id})
    files: dict[int, list[dict]] = {i: [] for i in ids}
    for f in s.scalars(select(TrackFile).where(TrackFile.track_id.in_(ids)).order_by(TrackFile.id)):
        files[f.track_id].append(
            {
                "id": f.id,
                "path": str(f.library_path or f.source_path),
                "bitrate": f.bitrate,
                "is_main": f.is_main,
                "bind_method": f.bind_method.value if f.bind_method else None,
                "bind_confidence": f.bind_confidence,
                "recording_mbid": f.recording_mbid,
            }
        )

    n = func.count(TrackAliasScrobble.id)
    alias_rows = s.execute(
        select(TrackAlias, n.label("n"))
        .outerjoin(TrackAliasScrobble, TrackAliasScrobble.alias_id == TrackAlias.id)
        .where(TrackAlias.track_id.in_(ids))
        .group_by(TrackAlias.id)
        .order_by(n.desc(), TrackAlias.id)
        .limit(ALIAS_CAP)
    ).all()
    aliases_total = s.scalar(select(func.count()).select_from(TrackAlias).where(TrackAlias.track_id.in_(ids)))
    plays = s.scalar(select(func.count()).select_from(TrackPlay).where(TrackPlay.track_id.in_(ids)))
    playlists = s.execute(
        select(Playlist.id, Playlist.name)
        .where(
            Playlist.id.in_(
                select(distinct(PlaylistTrack.playlist_id)).where(PlaylistTrack.track_id.in_(ids))
            )
        )
        .order_by(Playlist.name, Playlist.id)
    ).all()
    verdict = group_verdicts(s).get(root.id)

    return {
        "canon": {"id": root.id, "title": root.title, "artist": root.artist, "album": root.album},
        "members": [_member(t, sources[t.id], files[t.id]) for t in group],
        "aliases": [
            {
                "id": a.id,
                "title": a.title,
                "artist": a.artist,
                "album": a.album,
                "match_method": a.match_method.value if a.match_method else None,
                "match_confidence": a.match_confidence,
                "scrobbles": cnt,
            }
            for a, cnt in alias_rows
        ],
        "aliases_total": aliases_total or 0,
        "plays": plays or 0,
        "playlists": [{"id": pid, "name": name} for pid, name in playlists],
        "decisions": _decisions(s, group, resolution),
        "verdict": verdict.value if verdict else None,
    }


def _candidates(s: Session, title: str | None, artist_norm: str, album: str | None) -> list[dict]:
    """The top bind candidates for one unmatched row: distinct canon groups, best score first."""
    found = find_matches(
        s,
        normalize_name(title),
        artist_norm,
        normalize_name(album) or None,
        threshold=CAND_THRESHOLD,
        title_threshold=CAND_TITLE_THRESHOLD,
        limit=CANDIDATES * 2,  # twins of one canon collapse below, so over-fetch
    )
    out: list[dict] = []
    seen: set[int] = set()
    for t, score in found:
        canon = t.canon or t  # canon_id is terminal: one hop reaches the root
        if canon.id in seen:
            continue
        seen.add(canon.id)
        out.append(
            {
                "track_id": canon.id,
                "title": canon.title,
                "artist": canon.artist,
                "album": canon.album,
                "score": round(score, 3),
            }
        )
    return out[:CANDIDATES]


def unmatched_aliases(
    s: Session, cursor: str | None = None, limit: int = 50, query: str | None = None
) -> dict:
    """Unbound scrobbled aliases collapsed like the wanted list, each with its top bind candidates."""
    # Re-sorted in Python: SQL orders text by the DB collation, and bisecting needs Python's own order.
    groups = sorted(unmatched_groups(s, query), key=_unmatched_key)
    w = window("unmatched_aliases", groups, _unmatched_key, cursor, limit)
    rows = [
        {
            "alias_ids": g.alias_ids,
            "title": g.title,
            "artist": g.artist,
            "album": g.album,
            "scrobbles": g.scrobbles,
            "rejected": g.rejected,
            "candidates": _candidates(s, g.title, g.artist_norm, g.album),
        }
        for g in w.items
    ]
    return {"rows": rows, "total": w.total, "next_cursor": w.next_cursor}


def _unmatched_key(g: UnmatchedGroup) -> Key:
    """Most scrobbles first, then title and artist norms: the `unmatched_groups` order, unique per group."""
    return (-g.scrobbles, g.title_norm, g.artist_norm)
