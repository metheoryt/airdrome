from sqlmodel import Session, select, func

from jellyfist.conf import settings
from jellyfist.models import Track
from .schemas import DupGroup

DUPES = DupGroup.load(settings.duplicates_filepath)


def deduplicate_group(human_key: str, tracks: list[Track], s: Session) -> DupGroup:
    key = ",".join([str(t.track_id) for t in tracks])
    dg: DupGroup | None = None

    # cached choices
    if key in DUPES:
        dg: DupGroup = DUPES[key]
        # if [t.track_id for t in tracks] != dg.members:
        #     # handle possible divergence
        #     dg = None

    if dg:
        print("cached:", human_key)
    else:
        print("duplicates:", human_key)
        for i, t in enumerate(tracks):
            t: Track
            print(f"{i + 1}.", t.short_info)
        p = input("What to keep? Enter indices, separated by spaces: ")
        indices = {int(i) - 1 for i in p.strip().split()}

        dg = DupGroup(
            members=[t.track_id for t in tracks],
            keep=[i in indices for i in range(len(tracks))],
        )
        DUPES[key] = dg

    for track, keep in zip(tracks, dg.keep):
        # track list and dup group list are guaranteed to be identical at this point.
        # handle playlist and track file relations.
        if not keep:
            # TODO delete the files? Re-link the files?
            # TODO re-link to playlists?
            s.delete(track)
            s.flush()
            print("deleted:", track.artist_album_name)

    return dg


def deduplicate_tracks(s: Session):
    for cols in (
        (Track.artist_norm, Track.name_norm),
        (Track.album_artist_norm, Track.name_norm),
        (Track.album_norm, Track.name_norm),
    ):
        print(f"deduplicating by", "/".join([c.name for c in cols]))
        combinations = s.exec(
            select(*cols, func.count(Track.track_id).label("count"))
            .group_by(*cols)
            .having(func.count(Track.track_id) > 1)
            .order_by(*cols)
        )

        try:
            for *col_vals, count in combinations:
                col_to_val = zip(cols, col_vals)
                tracks = s.exec(
                    select(Track).where(*[v[0] == v[1] for v in col_to_val]).order_by(Track.track_id)
                ).all()
                key = "|".join(col_vals)
                deduplicate_group(key, list(tracks), s)

            s.commit()  # commit deletion of duplicate tracks
            print("tracks are deleted permanently")
            # save choices permanently
            DupGroup.dump(DUPES, settings.duplicates_filepath)

        except KeyboardInterrupt:
            # save choices permanently on ctrl+c
            DupGroup.dump(DUPES, settings.duplicates_filepath)
            raise
