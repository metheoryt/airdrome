from dataclasses import dataclass
from itertools import chain
from typing import Iterator

from rich.progress import Progress, TaskID
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.enums import Platform
from airdrome.models import AwareDatetime, Playlist, PlaylistTrack, Track, TrackFile

from .models import AppleFSDiscoverable, AppleMSPlaylist, AppleMSTrack, ApplePlaylist, AppleTrack


def _bind_track_files(apple_track: AppleFSDiscoverable, s: Session) -> list[TrackFile]:
    tfs = []
    for rel_path in apple_track.possible_locations(max_suffix=2):
        tf: TrackFile | None = s.scalars(
            select(TrackFile).where(TrackFile.source_path.contains(rel_path))
        ).one_or_none()
        if tf and tf.track_id is None:
            tfs.append(tf)
    return tfs


def _unify_xml_tracks(s: Session) -> Iterator[tuple[bool, bool, int]]:
    for apple_track in s.scalars(select(AppleTrack).where(AppleTrack.track_id.is_(None))):
        track_defaults = {
            "track_n": apple_track.track_number,
            "disc_n": apple_track.disc_number,
            "compilation": apple_track.compilation,
            "year": apple_track.year,
            "duration": round(apple_track.total_time / 1000) if apple_track.total_time else None,
            "loved": apple_track.loved if apple_track.loved else None,
            "album_loved": apple_track.album_loved if apple_track.album_loved else None,
            "rating": apple_track.rating if not apple_track.rating_computed else None,
            "album_rating": apple_track.album_rating if not apple_track.album_rating_computed else None,
            "date_added": apple_track.date_added,
        }
        track, track_created = Track.get_or_create(
            s,
            title=apple_track.name,
            artist=apple_track.artist,
            album=apple_track.album,
            album_artist=apple_track.album_artist,
            defaults=track_defaults,
        )
        track_updated = not track_created and track.fill_nulls(track_defaults)
        apple_track.track = track

        n_files = 0
        if not apple_track.apple_music:
            tfs = _bind_track_files(apple_track, s)
            for tf in tfs:
                track.files.append(tf)
            n_files = len(tfs)

        s.flush()
        yield track_created, track_updated, n_files


def _unify_ms_tracks(s: Session) -> Iterator[tuple[bool, bool, int]]:
    for ms_track in s.scalars(select(AppleMSTrack)):
        ms_track: AppleMSTrack
        duration_ms = ms_track.duration
        track_defaults = {
            "track_n": ms_track.track_number,
            "disc_n": ms_track.disc_number,
            "compilation": ms_track.compilation,
            "year": ms_track.year,
            "duration": round(duration_ms / 1000) if duration_ms else None,
            "date_added": ms_track.date_added,
        }
        track, track_created = Track.get_or_create(
            s,
            title=ms_track.title,
            artist=ms_track.artist,
            album=ms_track.album,
            album_artist=ms_track.album_artist,
            defaults=track_defaults,
        )
        track_updated = not track_created and track.fill_nulls(track_defaults)

        if ms_track.track_id is None:
            ms_track.track = track

        n_files = 0
        if ms_track.audio_file_extension:
            tfs = _bind_track_files(ms_track, s)
            for tf in tfs:
                track.files.append(tf)
            n_files = len(tfs)

        s.flush()
        yield track_created, track_updated, n_files


def unify_apple_tracks(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int, int]:
    """
    Create canonical Track records from AppleTrack and AppleMSTrack data,
    then bind matching TrackFile records via possible_locations() DB lookup.
    Returns (created, updated, files_bound) Track counts.
    """
    created = updated = files_bound = 0
    for was_created, was_updated, n_files in chain(_unify_xml_tracks(s), _unify_ms_tracks(s)):
        created += was_created
        updated += was_updated
        files_bound += n_files
        if progress is not None:
            progress.update(task, advance=1, created=created, updated=updated, files_bound=files_bound)
    return created, updated, files_bound


@dataclass
class _SourcePlaylist:
    name: str
    date_modified: AwareDatetime | None
    date_added: AwareDatetime | None
    description: str | None
    platform: Platform
    source_id: str
    track_ids: list[int]


def _gather_xml_source_playlists(s: Session) -> list[_SourcePlaylist]:
    result = []
    stmt = select(ApplePlaylist).where(~ApplePlaylist.master, ~ApplePlaylist.music, ~ApplePlaylist.folder)
    for pl in s.scalars(stmt):
        track_dates = [m.track.date_added for m in pl.members if m.track.date_added is not None]
        track_ids = [
            m.track.track_id
            for m in sorted(pl.members, key=lambda m: m.position)
            if m.track.track_id is not None
        ]
        result.append(
            _SourcePlaylist(
                name=pl.name,
                date_modified=max(track_dates) if track_dates else None,
                date_added=min(track_dates) if track_dates else None,
                description=pl.description or None,
                platform=Platform.APPLE,
                source_id=pl.persistent_id,
                track_ids=track_ids,
            )
        )
    return result


def _gather_ms_source_playlists(s: Session) -> list[_SourcePlaylist]:
    result = []
    for pl in s.scalars(select(AppleMSPlaylist)):
        track_ids = [
            m.track.track_id
            for m in sorted(pl.members, key=lambda m: m.position)
            if m.track.track_id is not None
        ]
        result.append(
            _SourcePlaylist(
                name=pl.title,
                date_modified=pl.items_modified_date,
                date_added=pl.date_added,
                description=None,
                platform=Platform.APPLE,
                source_id=str(pl.container_identifier),
                track_ids=track_ids,
            )
        )
    return result


def unify_apple_playlists(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int]:
    """
    Create deduplicated canonical Playlist records from Apple XML and Media Services data.
    Processes newest-to-oldest by date_modified; same-name playlists merge (unique tracks
    appended); playlists whose track set duplicates an existing canonical are skipped.
    Returns (playlists_created, tracks_linked).
    """
    existing = list(s.scalars(select(Playlist)))
    name_to_canonical: dict[str, Playlist] = {pl.name: pl for pl in existing}

    # Mutable per-canonical track-ID sets; updated in-place as we merge
    canonical_track_ids: dict[int, set[int]] = {
        pl.id: {
            pt.track_id for pt in s.scalars(select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id))
        }
        for pl in existing
    }

    sources = _gather_xml_source_playlists(s) + _gather_ms_source_playlists(s)
    # Newest date_modified first; nulls sorted last
    sources.sort(
        key=lambda p: (p.date_modified is None, -p.date_modified.timestamp() if p.date_modified else 0)
    )

    playlists_created = tracks_linked = 0

    for src in sources:
        if not src.track_ids:
            if progress is not None:
                progress.update(task, advance=1)
            continue

        if src.name in name_to_canonical:
            canonical = name_to_canonical[src.name]
            existing_ids = canonical_track_ids[canonical.id]

            max_pos_row = s.scalars(
                select(PlaylistTrack)
                .where(PlaylistTrack.playlist_id == canonical.id)
                .order_by(PlaylistTrack.position.desc())
            ).first()
            next_pos = (max_pos_row.position + 1) if max_pos_row else 1

            for track_id in src.track_ids:
                if track_id not in existing_ids:
                    s.add(PlaylistTrack(playlist_id=canonical.id, track_id=track_id, position=next_pos))
                    existing_ids.add(track_id)
                    next_pos += 1
                    tracks_linked += 1

        else:
            src_track_set = frozenset(src.track_ids)
            if any(src_track_set == frozenset(ids) for ids in canonical_track_ids.values()):
                if progress is not None:
                    progress.update(task, advance=1)
                continue

            canonical = Playlist(
                name=src.name,
                platform=src.platform,
                source_id=src.source_id,
                description=src.description,
                date_added=src.date_added,
                date_modified=src.date_modified,
            )
            s.add(canonical)
            s.flush()

            name_to_canonical[src.name] = canonical
            canonical_track_ids[canonical.id] = set(src.track_ids)
            playlists_created += 1

            for pos, track_id in enumerate(src.track_ids, start=1):
                s.add(PlaylistTrack(playlist_id=canonical.id, track_id=track_id, position=pos))
                tracks_linked += 1

        s.flush()
        if progress is not None:
            progress.update(task, advance=1, pl_created=playlists_created, tr_linked=tracks_linked)

    return playlists_created, tracks_linked
