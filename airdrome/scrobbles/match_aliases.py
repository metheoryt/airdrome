from collections.abc import Callable

from rich.progress import TextColumn
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from airdrome.console import done, is_verbose, make_progress
from airdrome.enums import BindMethod
from airdrome.library.ladder import build_mbid_index, recording_of_track
from airdrome.match import find_best_match
from airdrome.models import Track, TrackAlias, TrackAliasScrobble


def _unanimous(values: set[str | None]) -> str | None:
    """The one id all non-null ``values`` agree on; None if there is none or they disagree."""
    ids = {v for v in values if v}
    return ids.pop() if len(ids) == 1 else None


def alias_ids(s: Session, alias: TrackAlias) -> tuple[str | None, str | None]:
    """``(submitted, mapped)`` recording ids the alias's scrobbles unanimously carry.

    Scrobbles without an id neither contribute nor veto; two scrobbles naming different ids
    cancel that kind of id out (the alias is one text, so conflicting listens prove nothing).
    """
    rows = s.execute(
        select(TrackAliasScrobble.submitted_mbid, TrackAliasScrobble.mapped_mbid).where(
            TrackAliasScrobble.alias_id == alias.id
        )
    ).all()
    return _unanimous({r[0] for r in rows}), _unanimous({r[1] for r in rows})


def _exact_track(s: Session, alias: TrackAlias, prefer_mbid: str | None) -> Track | None:
    """The group canon of the track whose normalized title+artist(+album) equal the alias's, if any.

    All exact candidates tie at 1.0, so ``prefer_mbid`` (a mapped id, weak) may choose among them.
    """
    if not (alias.title_norm and alias.artist_norm):
        return None  # without an artist, equality proves nothing; the fuzzy rung handles it
    stmt = select(Track).where(
        Track.title_norm == alias.title_norm,
        or_(Track.artist_norm == alias.artist_norm, Track.album_artist_norm == alias.artist_norm),
    )
    if alias.album_norm:
        stmt = stmt.where(Track.album_norm == alias.album_norm)
    # Canons first, then the oldest row, so the pick is stable across runs.
    candidates = s.scalars(stmt.order_by(Track.canon_id.asc().nulls_first(), Track.id)).all()
    if not candidates:
        return None
    track = next(
        (c for c in candidates if prefer_mbid and recording_of_track(c) == prefer_mbid), candidates[0]
    )
    return track.canon or track


def _bind_alias(alias: TrackAlias, track: Track, method: BindMethod, confidence: float) -> None:
    """Record the binding of ``alias`` to ``track`` with its method and confidence."""
    alias.track = track
    alias.match_method = method
    alias.match_confidence = confidence


def do_match_aliases(
    s: Session,
    threshold: float = 0.4,
    on_progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """
    Sets TrackAlias.track_id for unmatched aliases. Returns (matched, unmatched).

    Ladder per alias: player-submitted recording id found in the library -> exact normalized tags ->
    trigram fuzzy (``find_best_track``). A service-mapped id never binds; it may only add 0.1 to a
    fuzzy pick's confidence (when it is that pick's recording) or break a tie between equally scored
    candidates. The threshold is decided on the unboosted score.

    on_progress(matched, unmatched) is called after each alias is processed.
    Testable directly — no session creation, no progress output.
    """
    aliases = s.scalars(select(TrackAlias).where(TrackAlias.track_id.is_(None))).all()
    by_mbid = build_mbid_index(s)  # only the id map; the fingerprint index is not needed here
    matched = unmatched = 0

    for alias in aliases:
        submitted, mapped = alias_ids(s, alias)
        if submitted and (candidates := by_mbid.get(submitted)):
            # Several tracks on one recording are one group by definition; the lowest id anchors it.
            lowest = candidates[0]
            _bind_alias(alias, lowest.canon or lowest, BindMethod.MBID, 1.0)
        elif exact := _exact_track(s, alias, mapped):
            _bind_alias(alias, exact, BindMethod.TAGS_EXACT, 1.0)
        elif found := find_best_match(
            s,
            alias.title_norm,
            alias.artist_norm,
            alias.album_norm,
            threshold=threshold,
            log=log,
            prefer_mbid=mapped,
        ):
            track, score = found
            confirmed = mapped is not None and recording_of_track(track) == mapped
            _bind_alias(alias, track, BindMethod.TAGS_FUZZY, min(1.0, score + 0.1) if confirmed else score)

        if alias.track is not None:
            matched += 1
            if matched % 100 == 0:
                s.flush()
        else:
            unmatched += 1

        if on_progress:
            on_progress(matched, unmatched)

    s.flush()
    return matched, unmatched


def match_aliases(s: Session, threshold: float = 0.4) -> None:
    """Match unmatched aliases to canonical tracks, with a progress bar and summary."""
    progress = make_progress(
        TextColumn("[green]✓ {task.fields[match]}[/green]"),
        TextColumn("[red]✗ {task.fields[mismatch]}[/red]"),
    )

    total = s.scalars(select(func.count(TrackAlias.id)).where(TrackAlias.track_id.is_(None))).one()

    with progress:
        task = progress.add_task("Matching aliases", total=total, match=0, mismatch=0)

        def _on_progress(matched: int, unmatched: int) -> None:
            progress.update(task, advance=1, match=matched, mismatch=unmatched)

        matched, unmatched = do_match_aliases(
            s,
            threshold=threshold,
            on_progress=_on_progress,
            # Per-alias match reasoning is verbose-only; otherwise it floods the bar.
            log=progress.console.print if is_verbose() else None,
        )

    done(f"matched {matched}, unmatched {unmatched}")
