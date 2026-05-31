from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from .models import Track


ColVal = tuple[Any, str]
ColOptVal = tuple[Any, str | None]

# Minimum normalized-title length for the substring-containment signal. Below this, short
# fragments ("love", "intro") are contained in too many unrelated titles to be a useful
# signal; there the artist/album score is the only guard, so we fall back to exact/fuzzy.
MIN_CONTAINMENT_LEN = 4


def build_match_score(artist_norm, album_norm):
    """Weighted artist+album trigram similarity of a candidate Track to an alias (0..1)."""
    if artist_norm:
        artist_sim_expr = func.greatest(
            func.similarity(Track.artist_norm, artist_norm),
            func.similarity(Track.album_artist_norm, artist_norm),
        )
    else:
        # alias has no artist → perfect match
        artist_sim_expr = 1.0

    if album_norm:
        album_sim_expr = func.similarity(Track.album_norm, album_norm)
    else:
        album_sim_expr = 0.5 if artist_norm else 1.0

    artist_w = 0.75
    album_w = 0.25

    weighted_sum = artist_sim_expr * artist_w + album_sim_expr * album_w
    weight_sum = artist_w + album_w
    score = weighted_sum / weight_sum

    return score


def _title_candidate_clause(title_norm: str, title_threshold: float):
    """Predicate accepting tracks whose title is exact, trigram-similar, or containment-related.

    Containment (one normalized title is a substring of the other) catches the common case
    where a source dropped or added a suffix — 'none shall pass' vs 'none shall pass radio
    edit', 'clarity' vs 'clarity!' — which trigram similarity alone scores too low to recognise.
    """
    similar = func.similarity(Track.title_norm, title_norm) >= title_threshold
    contained = and_(
        func.length(title_norm) >= MIN_CONTAINMENT_LEN,
        func.length(Track.title_norm) >= MIN_CONTAINMENT_LEN,
        or_(
            func.strpos(Track.title_norm, title_norm) > 0,
            func.strpos(literal(title_norm), Track.title_norm) > 0,
        ),
    )
    return or_(Track.title_norm == title_norm, similar, contained)


def find_best_track(
    session: Session,
    title_norm,
    artist_norm,
    album_norm,
    threshold: float = 0.4,
    title_threshold: float = 0.45,
    log: Callable[[str], None] | None = None,
) -> Track | None:
    """Find the best canonical Track for an alias, gating on a fuzzy title and artist/album score."""
    if not title_norm:
        return None

    if not artist_norm:
        # No artist to corroborate a relaxed title: `build_match_score` would treat the
        # missing artist as a perfect 1.0, so a fuzzy title would mis-attach generic
        # same-ish titles ("crickets in the rain" -> "caught in the rain"). Require an
        # exact title and pick the best same-title track. Album, if any, only breaks ties.
        stmt = (
            select(Track)
            .where(Track.title_norm == title_norm)
            .order_by(
                Track.canon_id.asc().nulls_first(),  # canonical (NULL canon_id) first
                Track.album_artist_norm,
                Track.artist_norm,
                Track.album_norm,
            )
            .limit(1)
        )
        return session.scalars(stmt).one_or_none()

    # With an artist signal we can loosen the title side: trigram-filter candidates
    # (GIN-indexed via `%`), keep those whose title is exact / similar / containment-related,
    # then rank by the artist+album score. Artist weight (0.75) still rejects same-title,
    # different-artist collisions, so the relaxed title gate stays safe.
    score = build_match_score(artist_norm, album_norm).label("score")
    title_sim = func.similarity(Track.title_norm, title_norm)
    stmt = (
        select(Track, score)
        .where(Track.title_norm.op("%")(title_norm))
        .where(_title_candidate_clause(title_norm, title_threshold))
        .order_by(score.desc(), title_sim.desc())
        .limit(1)
    )
    result = session.execute(stmt).one_or_none()
    if not result:
        return None
    track, score_val = result

    if score_val < threshold:
        return None
    if score_val < threshold + 0.1 and log:
        alias_l = f"{title_norm[:25]:<25} | {album_norm[:40]:<40} | {artist_norm[:25]:<25}"
        track_l = (
            f"{track.title_norm[:25]:<25} | "
            f"{track.album_norm[:40]:<40} | "
            f"{track.artist_norm[:25]:<25} | "
            f"{track.album_artist_norm[:25]:<25}"
        )
        log(f"[dim]low confidence {score_val:.03f}[/dim]")
        log(f"[dim]  alias: {alias_l}[/dim]")
        log(f"[dim]  track: {track_l}[/dim]")

    return track
