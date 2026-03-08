from typing import Any

from sqlmodel import Column

ColVal = tuple[Any, str]
ColOptVal = tuple[Any, str | None]


def generate_match_filter_sets(
    title: ColVal, artist: ColOptVal, album: ColOptVal, album_artist_col: Any | None = None
):
    """
    Generate a set of filters for given columns and their respective value.

    Go from stricter filtering to broader.
    """
    filtersets = []
    filters = [
        (title, artist, album),
        (title, artist, None),
        (title, None, album),
    ]
    if not album[1] and not artist[1]:
        # filter by title-only aliases that have title only
        filters.append((title, None, None))

    artist_cols = [artist[0]]
    if album_artist_col:
        artist_cols.append(album_artist_col)

    # equals
    for title, artist, album in filters:
        for artist_col in artist_cols:
            filterset = [title[0] == title[1]]
            if artist:
                filterset.append(artist_col == artist[1])
            if album:
                filterset.append(album[0] == album[1])
            filtersets.append(tuple(filterset))

    return list(dict.fromkeys(filtersets))
