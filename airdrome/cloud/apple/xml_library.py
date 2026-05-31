from rich.progress import Progress, TaskID
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourcePlaylistTrack, SourceTrack
from airdrome.enums import Source

from .schemas import ApplePlaylistImport


# Maps Apple's iTunes-XML keys to SourceTrack column names; unmapped/non-column targets
# (apple_music, location, kind, sort_*, …) are preserved in SourceTrack.extra.
_TRACK_ALIAS_MAP = {
    "Track ID": "apple_track_id",
    "Name": "title",
    "Album": "album",
    "Artist": "artist",
    "Album Artist": "album_artist",
    "Apple Music": "apple_music",
    "Compilation": "compilation",
    "Track Number": "track_number",
    "Disc Number": "disc_number",
    "Year": "year",
    "Release Date": "release_date",
    "Loved": "loved",
    "Favorited": "favorited",
    "Rating": "rating",
    "Rating Computed": "rating_computed",
    "Album Loved": "album_loved",
    "Album Rating": "album_rating",
    "Album Rating Computed": "album_rating_computed",
    "Date Added": "date_added",
    "Date Modified": "date_modified",
    "Play Date UTC": "play_date_utc",
    "Play Date": "play_date",
    "Total Time": "duration_ms",
    "Size": "size",
    "Track Type": "track_type",
    "Persistent ID": "persistent_id",
    "Kind": "kind",
    "Grouping": "grouping",
    "Genre": "genre",
    "Location": "location",
    "Bit Rate": "bit_rate",
    "Sample Rate": "sample_rate",
    "BPM": "bpm",
    "Normalization": "normalization",
    "Volume Adjustment": "volume_adjustment",
    "Play Count": "play_count",
    "Skip Count": "skip_count",
    "Skip Date": "skip_date",
    "Disliked": "disliked",
    "Comments": "comments",
    "Sort Name": "sort_name",
    "Sort Artist": "sort_artist",
    "Sort Album Artist": "sort_album_artist",
    "Sort Album": "sort_album",
    "Sort Composer": "sort_composer",
    "Work": "work",
    "Composer": "composer",
    "Movement Name": "movement_name",
    "Movement Count": "movement_count",
    "Disc Count": "disc_count",
    "Track Count": "track_count",
    "Artwork Count": "artwork_count",
    "File Folder Count": "file_folder_count",
    "Library Folder Count": "library_folder_count",
    "Protected": "protected",
    "Music Video": "music_video",
    "Has Video": "has_video",
    "Part Of Gapless Album": "part_of_gapless_album",
    "Playlist Only": "playlist_only",
    "Purchased": "purchased",
    "Matched": "matched",
    "Explicit": "explicit",
    "Clean": "clean",
    "HD": "hd",
}


def do_import_tracks(
    s: Session,
    tracks_data: dict,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> int:
    """
    Import Apple Music tracks into the database. Returns number of new SourceTrack records created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for data in tracks_data.values():
        source_id = str(data["Track ID"])

        if s.scalars(
            select(SourceTrack).where(
                SourceTrack.provider == Source.APPLE_XML, SourceTrack.source_id == source_id
            )
        ).one_or_none():
            if progress is not None:
                progress.advance(task_id)
            continue

        st = SourceTrack.from_raw(Source.APPLE_XML, data["Track ID"], data, alias_map=_TRACK_ALIAS_MAP)
        s.add(st)
        s.flush()
        created += 1

        if progress is not None:
            progress.update(task_id, advance=1, created=created)

    return created


def do_import_playlists(
    s: Session,
    playlists_data: list,
    *,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> int:
    """
    Import Apple Music playlists into the database. Returns number of new playlists created.

    Testable directly — no session creation, no progress output.
    """
    created = 0
    for pl in playlists_data:
        pl_import = ApplePlaylistImport(**pl)

        if progress is not None:
            progress.update(task_id, description=pl_import.name)

        # Skip smart playlists and iTunes' internal Library/Music containers.
        if pl_import.smart_info or pl_import.master or pl_import.music:
            if progress is not None:
                progress.advance(task_id)
            continue

        pl_db = s.scalars(
            select(SourcePlaylist).where(
                SourcePlaylist.provider == Source.APPLE_XML,
                SourcePlaylist.source_id == pl_import.persistent_id,
            )
        ).one_or_none()
        if not pl_db:
            dump = pl_import.model_dump(exclude={"smart_info", "smart_criteria", "items"})
            pl_db = SourcePlaylist(
                provider=Source.APPLE_XML,
                source_id=dump.pop("persistent_id"),
                name=dump.pop("name"),
                description=dump.pop("description") or None,
                folder=dump.pop("folder"),
                extra=dump,  # master, music, and the rest preserved here
            )
            s.add(pl_db)
            s.flush()
            created += 1

        existing_track_ids = {
            link.track_id
            for link in s.scalars(
                select(SourcePlaylistTrack).where(SourcePlaylistTrack.playlist_id == pl_db.id)
            )
        }
        seen = set()
        pl_track_source_ids = [str(v.apple_track_id) for v in pl_import.items]
        pl_tracks = {
            t.source_id: t
            for t in s.scalars(
                select(SourceTrack).where(
                    SourceTrack.provider == Source.APPLE_XML,
                    SourceTrack.source_id.in_(pl_track_source_ids),
                )
            )
        }
        pos = 0
        for pls_track in pl_import.items:
            sid = str(pls_track.apple_track_id)
            if sid in seen:
                continue

            pos += 1
            source_track = pl_tracks.get(sid)
            if source_track is None:
                continue
            if source_track.id not in existing_track_ids:
                apt = SourcePlaylistTrack(track=source_track, playlist=pl_db, position=pos)
                s.add(apt)
            seen.add(sid)
        s.flush()

        if progress is not None:
            progress.advance(task_id)

    return created
