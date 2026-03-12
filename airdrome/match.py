from typing import Any

from sqlmodel import Session, func, select

from .models import Track


ColVal = tuple[Any, str]
ColOptVal = tuple[Any, str | None]


def build_match_score(artist_norm, album_norm):
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
        if artist_norm:
            album_sim_expr = 0.5
        else:
            album_sim_expr = 1.0

    artist_w = 0.75
    album_w = 0.25

    weighted_sum = artist_sim_expr * artist_w + album_sim_expr * album_w
    weight_sum = artist_w + album_w
    score = weighted_sum / weight_sum

    return score


def find_best_track(
    session: Session, title_norm, artist_norm, album_norm, threshold: float = 0.4
) -> Track | None:

    if not artist_norm and not album_norm:
        stmt = (
            select(Track)
            .where(Track.title_norm == title_norm)
            .order_by(
                Track.canon_id,  # originals first
                Track.album_artist_norm,
                Track.artist_norm,
                Track.album_norm,
            )
            .limit(1)
        )
        track, score_val = session.exec(stmt).one_or_none(), 1.0
    else:
        score = build_match_score(artist_norm, album_norm).label("score")
        stmt = select(Track, score).where(Track.title_norm == title_norm).order_by(score.desc()).limit(1)

        result = session.exec(stmt).one_or_none()
        if result:
            track, score_val = result
        else:
            track, score_val = None, 0.0

    if not track:
        return None

    if score_val < threshold:
        return None
    if score_val < threshold + 0.1:
        alias_l = f"{title_norm[:25]:<25} | {album_norm[:40]:<40} | {artist_norm[:25]:<25}"
        track_l = (
            f"{track.title_norm[:25]:<25} | "
            f"{track.album_norm[:40]:<40} | "
            f"{track.artist_norm[:25]:<25} | "
            f"{track.album_artist_norm[:25]:<25}"
        )
        print(f"alias {score_val:.03f}:", alias_l)
        print(f"track {score_val:.03f}:", track_l)
        print()

    return track
