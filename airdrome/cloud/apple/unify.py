from rich.progress import Progress, TaskID
from sqlmodel import Session, select

from airdrome.enums import Platform
from airdrome.models import Playlist, PlaylistTrack, Track, TrackFile

from .models import (
    AppleFSDiscoverable,
    AppleMediaServicesPlaylist,
    AppleMediaServicesTrack,
    ApplePlaylist,
    AppleTrack,
)


def _bind_track_files(apple_track: AppleFSDiscoverable, s: Session) -> list[TrackFile]:
    tfs = []
    for rel_path in apple_track.possible_locations(max_suffix=2):
        tf: TrackFile | None = s.exec(
            select(TrackFile).where(TrackFile.source_path.contains(rel_path))
        ).one_or_none()
        if tf and tf.track_id is None:
            tfs.append(tf)
    return tfs


def _unify_xml_tracks(s: Session, progress: Progress, task: TaskID) -> tuple[int, int, int]:
    created = updated = files_bound = 0
    for apple_track in s.exec(select(AppleTrack).where(AppleTrack.track_id.is_(None))):
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
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        apple_track.track = track

        if not apple_track.apple_music:
            tfs = _bind_track_files(apple_track, s)
            for tf in tfs:
                track.files.append(tf)
            files_bound += len(tfs)

        s.flush()
        progress.update(task, advance=1, created=created, updated=updated, files_bound=files_bound)

    return created, updated, files_bound


def _unify_ms_tracks(s: Session, progress: Progress, task: TaskID) -> tuple[int, int, int]:
    created = updated = files_bound = 0
    for ms_track in s.exec(select(AppleMediaServicesTrack)):
        ms_track: AppleMediaServicesTrack
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
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        if ms_track.track_id is None:
            ms_track.track = track

        if ms_track.audio_file_extension:
            tfs = _bind_track_files(ms_track, s)
            for tf in tfs:
                track.files.append(tf)
            files_bound += len(tfs)

        s.flush()
        progress.update(task, advance=1, created=created, updated=updated, files_bound=files_bound)

    return created, updated, files_bound


def unify_apple_tracks(s: Session, progress: Progress, task: TaskID) -> tuple[int, int, int]:
    """
    Create canonical Track records from AppleTrack and AppleMediaServicesTrack data,
    then bind matching TrackFile records via possible_locations() DB lookup.
    Returns (created, updated, files_bound) Track counts.
    """
    xml_created, xml_updated, xml_files = _unify_xml_tracks(s, progress, task)
    ms_created, ms_updated, ms_files = _unify_ms_tracks(s, progress, task)
    return xml_created + ms_created, xml_updated + ms_updated, xml_files + ms_files


def _unify_xml_playlists(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    playlists_created = tracks_linked = 0

    stmt = select(ApplePlaylist).where(~ApplePlaylist.master, ~ApplePlaylist.music, ~ApplePlaylist.folder)
    for pl in s.exec(stmt):
        playlist, pl_created = Playlist.get_or_create(
            s,
            platform=Platform.APPLE,
            source_id=pl.persistent_id,
            defaults={"name": pl.name, "description": pl.description or None},
        )
        if pl_created:
            playlists_created += 1

        existing_track_ids = {
            pt.track_id
            for pt in s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id))
        }

        for member in sorted(pl.members, key=lambda m: m.position):
            canon_track_id = member.track.track_id
            if canon_track_id is None or canon_track_id in existing_track_ids:
                continue
            s.add(PlaylistTrack(playlist_id=playlist.id, track_id=canon_track_id, position=member.position))
            existing_track_ids.add(canon_track_id)
            tracks_linked += 1

        s.flush()
        progress.update(task, advance=1, pl_created=playlists_created, tr_linked=tracks_linked)

    return playlists_created, tracks_linked


def _unify_ms_playlists(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    playlists_created = tracks_linked = 0

    for pl in s.exec(select(AppleMediaServicesPlaylist)):
        playlist, pl_created = Playlist.get_or_create(
            s,
            platform=Platform.APPLE,
            source_id=str(pl.container_identifier),
            defaults={"name": pl.title},
        )
        if pl_created:
            playlists_created += 1

        existing_track_ids = {
            pt.track_id
            for pt in s.exec(select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id))
        }

        for member in sorted(pl.members, key=lambda m: m.position):
            canon_track_id = member.track.track_id
            if canon_track_id is None or canon_track_id in existing_track_ids:
                continue
            s.add(PlaylistTrack(playlist_id=playlist.id, track_id=canon_track_id, position=member.position))
            existing_track_ids.add(canon_track_id)
            tracks_linked += 1

        s.flush()
        progress.update(task, advance=1, pl_created=playlists_created, tr_linked=tracks_linked)

    return playlists_created, tracks_linked


def unify_apple_playlists(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    """
    Create canonical Playlist and PlaylistTrack records from Apple platform data.
    Returns (playlists_created, tracks_linked) counts.
    """
    xml_pl, xml_tr = _unify_xml_playlists(s, progress, task)
    ms_pl, ms_tr = _unify_ms_playlists(s, progress, task)
    return xml_pl + ms_pl, xml_tr + ms_tr
