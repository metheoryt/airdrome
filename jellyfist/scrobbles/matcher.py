from collections import Counter

from sqlalchemy import BinaryExpression
from sqlmodel import Session, select, update, or_

from jellyfist.models import TrackAlias, Track, engine


def get_filter_clauses(alias: TrackAlias) -> list[list[BinaryExpression]]:
    clauses = []
    filters: list[tuple[str, str | None, str | None]] = [
        (alias.title_norm, alias.artist_norm, alias.album_norm),
        (alias.title_norm, alias.artist_norm, None),
        (alias.title_norm, None, alias.album_norm),
    ]
    if not alias.album_norm and not alias.artist_norm:
        # filter by title-only aliases that have title only
        filters.append((alias.title_norm, None, None))

    # equals
    for title, artist, album in filters:
        for artist_col in (Track.artist_norm, Track.album_artist_norm):
            clause = [Track.name_norm == title]
            if artist is not None:
                clause.append(artist_col == artist)
            if album is not None:
                clause.append(Track.album_norm == album)
            clauses.append(tuple(clause))

    # LIKE (trgm index)
    for title, artist, album in filters:
        for artist_col in (Track.artist_norm, Track.album_artist_norm):
            if len(title) < 3:  # minimal length to filter by LIKE
                continue
            clause = [Track.name_norm.startswith(title)]

            # Artist/album still filters by exact match
            if artist is not None:
                clause.append(artist_col == artist)
            if album is not None:
                clause.append(Track.album_norm == album)
            clauses.append(tuple(clause))

    # make it unique without sacrificing order (3.7+)
    return list(dict.fromkeys(clauses))


class AliasMatcher:
    @classmethod
    def maybe_complete_alias(cls, alias: TrackAlias, s: Session):
        # implied that the title is not empty

        if alias.album_norm and alias.artist_norm:
            # no need to complete the data
            return []

        wheres = [TrackAlias.title_norm == alias.title_norm]

        or_wheres = []
        if not alias.artist_norm:
            or_wheres.append(TrackAlias.artist_norm != "")
        if not alias.album_norm:
            or_wheres.append(TrackAlias.album_norm != "")

        if or_wheres:
            wheres.append(or_(*or_wheres))

        matched_aliases = s.exec(select(TrackAlias).where(*wheres)).all()
        matched_artist = matched_album = None
        if not len(matched_aliases):
            # no matches
            return []

        elif len(matched_aliases) > 1:
            # multiple matches: combine the data
            artist_set = {ma.artist_norm for ma in matched_aliases if ma.artist_norm}
            album_set = {ma.album_norm for ma in matched_aliases if ma.album_norm}

            if len(artist_set) == 1:
                matched_artist = next(iter(artist_set))

            if len(album_set) == 1:
                matched_album = next(iter(album_set))
        else:
            # exactly one match
            match: TrackAlias = matched_aliases[0]
            matched_artist = match.artist_norm
            matched_album = match.album_norm

        changed = list()
        if not alias.album_norm and matched_album:
            alias.album_norm = matched_album
            changed.append("album")
        if not alias.artist_norm and matched_artist:
            alias.artist_norm = matched_artist
            changed.append("artist")

        return changed

    @staticmethod
    def match_alias(alias: TrackAlias, s: Session) -> tuple[int, list[Track]]:
        the_name_of_the_lord = get_filter_clauses(alias)
        i = 0
        for i, what in enumerate(the_name_of_the_lord):
            stmt = select(Track).where(*what)
            tracks = s.exec(stmt).all()
            if tracks:
                return i, list(tracks)
        return i, []

    @classmethod
    def match_all(cls, reset: bool = False):
        cnt = Counter()
        with Session(engine) as s:
            if reset:
                s.exec(update(TrackAlias).values(track_id=None))
                s.commit()
                print("dropped all alias-track links")

            total = matched = 0
            for alias in s.exec(select(TrackAlias)):
                alias: TrackAlias
                total += 1
                # try to complete the alias data first
                completed_fields = cls.maybe_complete_alias(alias, s)
                if completed_fields:
                    s.flush()  # for match to see it
                    print(f"completed with {', '.join(completed_fields):<15} {alias.repr}")

                i, tracks = cls.match_alias(alias, s)
                if len(tracks) == 1:
                    matched += 1
                    track: Track = tracks[0]
                    # print("Alias", alias.repr)
                    # print("Track", track.repr)
                    alias.track = track
                    cnt[i] += 1
                    if matched % 20 == 0:
                        s.commit()

                print(f"{matched:<6} of {total:<6} aliases matched", end="\r", flush=True)

            s.commit()
            print()
            print("match results (one track matched only):")
            for attempt, aliases_n in cnt.most_common():
                print(f"{attempt:<3} attempt:", aliases_n)
