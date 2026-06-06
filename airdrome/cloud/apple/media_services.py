from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourcePlaylistTrack, SourceTrack
from airdrome.enums import Source


_SKIP_PLAYLIST_TYPES = {"Smart Playlist", "Genius Mix", "Genius Playlist", "Folder"}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def import_ms_track(s: Session, item: dict) -> bool:
    """Import Apple Media Services tracks. Returns whether a SourceTrack record was created."""
    source_id = str(item["Track Identifier"])

    if s.scalars(
        select(SourceTrack).where(SourceTrack.provider == Source.APPLE_MS, SourceTrack.source_id == source_id)
    ).one_or_none():
        return False

    st = SourceTrack(
        provider=Source.APPLE_MS,
        source_id=source_id,
        title=item["Title"],
        artist=item.get("Artist"),
        album=item.get("Album"),
        album_artist=item.get("Album Artist"),
        compilation=item.get("Is Part of Compilation", False),
        track_number=item.get("Track Number On Album"),
        disc_number=item.get("Disc Number Of Album"),
        year=item.get("Track Year"),
        duration_ms=item.get("Track Duration"),
        date_added=_parse_dt(item.get("Date Added To Library")),
        date_modified=_parse_dt(item.get("Last Modified Date")),
        extra={
            "track_identifier": item["Track Identifier"],
            "track_count": item.get("Track Count On Album"),
            "disc_count": item.get("Disc Count Of Album"),
            "play_count": item.get("Track Play Count"),
            "skip_count": item.get("Skip Count"),
            "release_date": item.get("Release Date"),
            "genre": item.get("Genre"),
            "audio_file_extension": item.get("Audio File Extension") or None,
            "is_purchased": bool(item.get("Is Purchased", False)),
            "purchased_track_identifier": item.get("Purchased Track Identifier") or None,
            "audio_matched_track_identifier": item.get("Audio Matched Track Identifier") or None,
        },
    )
    s.add(st)
    return True


def import_ms_playlist(s: Session, pl: dict) -> bool:
    """Import Apple Media Services playlists. Returns whether a new playlist row was created."""
    container_id = pl["Container Identifier"]
    container_type = pl.get("Container Type", "")
    title = pl.get("Title", "")
    created = False

    if container_type in _SKIP_PLAYLIST_TYPES:
        return created

    item_identifiers = pl.get("Playlist Item Identifiers") or []
    if not item_identifiers:
        return created

    source_id = str(container_id)
    pl_db = s.scalars(
        select(SourcePlaylist).where(
            SourcePlaylist.provider == Source.APPLE_MS, SourcePlaylist.source_id == source_id
        )
    ).one_or_none()

    if not pl_db:
        pl_db = SourcePlaylist(
            provider=Source.APPLE_MS,
            source_id=source_id,
            name=title,
            date_added=_parse_dt(pl.get("Added Date")),
            date_modified=_parse_dt(pl.get("Playlist Items Modified Date")),
            extra={
                "container_type": container_type,
                "parent_folder_identifier": pl.get("Parent Folder Identifier"),
            },
        )
        s.add(pl_db)
        s.flush()
        created = True

    if not created:
        # clear all playlist members to insert again
        s.execute(delete(SourcePlaylistTrack).where(SourcePlaylistTrack.playlist_id == pl_db.id))

    # Apple's "Playlist Item Identifiers" can be badly inflated: an export was seen
    # listing 36,797 identifiers for a 206-track playlist (each early track repeated up
    # to 384x, a doubling artifact of Apple concatenating version snapshots). Dedup by
    # first occurrence — keeps order, kills the corruption. This loses any intentional
    # in-playlist repeat, but those are indistinguishable from the corruption and absent
    # in practice. dict.fromkeys preserves insertion order.
    member_source_ids = list(dict.fromkeys(str(i) for i in item_identifiers))
    ms_tracks = s.scalars(
        select(SourceTrack).where(
            SourceTrack.provider == Source.APPLE_MS, SourceTrack.source_id.in_(member_source_ids)
        )
    )
    ms_tracks_by_source_id = {t.source_id: t for t in ms_tracks}

    pos = 0
    for track_identifier in member_source_ids:
        if (ms_track := ms_tracks_by_source_id.get(track_identifier)) is None:
            # the track referenced in the playlist is not in the library, skip it
            continue

        pos += 1
        s.add(SourcePlaylistTrack(track=ms_track, playlist=pl_db, position=pos))

    s.flush()

    return created
