from dataclasses import dataclass
from typing import Iterator

from rich.progress import Progress, TaskID
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.console import console
from airdrome.enums import Source
from airdrome.models import AwareDatetime, Playlist, PlaylistTrack, Track, TrackFile


def _bind_track_files(source_track: SourceTrack, s: Session) -> list[TrackFile]:
    tfs = []
    for rel_path in source_track.possible_locations(max_suffix=2):
        tf: TrackFile | None = s.scalars(
            select(TrackFile).where(TrackFile.source_path.contains(rel_path))
        ).one_or_none()
        if tf and tf.track_id is None:
            tfs.append(tf)
    return tfs


def expects_local_file(st: SourceTrack) -> bool:
    """Whether a local audio file is expected on disk for this source track.

    File binding is attempted for every source track regardless; this helper only encodes the
    "a local copy should exist" expectation (XML tracks not added from Apple Music; MS tracks with
    a known audio extension) so a missing match can be surfaced.
    """
    if st.provider == Source.APPLE_XML:
        return not st.extra.get("apple_music", False)
    if st.provider == Source.APPLE_MS:
        return bool(st.extra.get("audio_file_extension"))
    return False


def _unify_source_tracks(s: Session) -> Iterator[tuple[bool, bool, int]]:
    for st in s.scalars(select(SourceTrack).where(SourceTrack.track_id.is_(None))):
        track_defaults = {
            "track_n": st.track_number,
            "disc_n": st.disc_number,
            "compilation": st.compilation,
            "year": st.year,
            "duration": round(st.duration_ms / 1000) if st.duration_ms else None,
            "loved": st.loved or None,
            "album_loved": st.album_loved or None,
            "rating": st.rating if not st.rating_computed else None,
            "album_rating": st.album_rating if not st.album_rating_computed else None,
            "date_added": st.date_added,
        }
        track, track_created = Track.get_or_create(
            s,
            title=st.title,
            artist=st.artist,
            album=st.album,
            album_artist=st.album_artist,
            defaults=track_defaults,
        )
        track_updated = not track_created and track.fill_nulls(track_defaults)
        st.track = track

        # Rely on FS discovery for everyone; the flag only tells us whether to complain on a miss.
        tfs = _bind_track_files(st, s)
        for tf in tfs:
            track.files.append(tf)
        n_files = len(tfs)
        if not tfs and expects_local_file(st):
            console.print(f"[dim yellow]expected local file not found: {st.title!r}[/dim yellow]")

        s.flush()
        yield track_created, track_updated, n_files


def unify_apple_tracks(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int, int]:
    """
    Create canonical Track records from SourceTrack data,
    then bind matching TrackFile records via possible_locations() DB lookup.
    Returns (created, updated, files_bound) Track counts.
    """
    created = updated = files_bound = 0
    for was_created, was_updated, n_files in _unify_source_tracks(s):
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
    platform: Source
    source_id: str
    track_ids: list[int]


def _gather_source_playlists(s: Session) -> list[_SourcePlaylist]:
    result = []
    stmt = select(SourcePlaylist).where(~SourcePlaylist.folder)
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
                # XML playlists carry no own dates → derive from members; MS supplies its own.
                date_modified=pl.date_modified or (max(track_dates) if track_dates else None),
                date_added=pl.date_added or (min(track_dates) if track_dates else None),
                description=pl.description or None,
                platform=pl.provider,
                source_id=pl.source_id,
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

    sources = _gather_source_playlists(s)
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
