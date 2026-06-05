"""Unify: build canonical ``Track`` and ``Playlist`` records from imported source data.

``do_unify`` runs three independent, idempotent stages in order. Each one turns *source*
rows (whatever a given import wrote) into the canonical records the rest of Airdrome uses:

1. **Source tracks** — every ``SourceTrack`` (Apple XML, Apple Media Services, …) becomes
   or joins a canonical ``Track`` keyed on title/artist/album/album_artist; then its on-disk
   copies are bound to it (see "binding files", below).
2. **Source playlists** — every non-folder ``SourcePlaylist`` becomes or merges into a
   canonical ``Playlist``, deduplicated both by name (merge) and by identical track set (skip).
3. **Orphan files** — ``TrackFile`` rows that *no* source claimed (e.g. scanned from disk but
   never imported) are adopted: matched to an existing canonical ``Track`` by their own tags,
   or made into one if no match exists.

Binding files — the subtle part
--------------------------------
A ``TrackFile`` carries both an on-disk ``source_path`` (named by the Apple app / a prior
organize run) and its own embedded tags (``title``/``artist``/…, read by ``enrich``). Those two
can disagree: tags get edited, go stale, or are missing entirely. So files reach their track
through **two complementary passes with different join keys**, in this order:

  1. By **filename** (stage 1, ``_bind_track_files``): ``possible_locations`` rebuilds the Apple
     on-disk path from the *source's* metadata and substring-matches ``source_path``. This binds
     a file to its source track even when the file's own tags are messy or absent.
  2. By **tags** (stage 3, ``_unify_orphan_files``): whatever the filename pass left unbound is
     adopted via the file's embedded tags.

Pass 1 is not redundant with pass 2: dropping it would re-route tag-divergent files into pass 2,
which would mint a *second* canonical track from the tags — leaving one track with the source
data and no file, and a duplicate with the file and no source data.

"Idempotent" means re-running unify only fills gaps: existing canonical records are reused and
their NULL fields backfilled, never duplicated. Stages 1 and 3 share ``_upsert_track`` for that
get-or-create-then-backfill logic; they differ only in where the metadata and files come from.
"""

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.console import console
from airdrome.enums import Source
from airdrome.models import AwareDatetime, Playlist, PlaylistTrack, Track, TrackFile


def _progress(summary: str) -> Progress:
    """A stage progress bar: description, bar, M-of-N, a per-stage summary column, elapsed."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(summary),
        TimeElapsedColumn(),
        console=console,
    )


def _upsert_track(s: Session, *, defaults: dict, **key) -> tuple[Track, bool, bool]:
    """Get-or-create a canonical Track by ``key``, backfilling NULLs from ``defaults``.

    Returns ``(track, created, updated)``: ``created`` if a new row was made, ``updated`` if an
    existing row had any NULL fields filled. Shared by the source-track and orphan-file stages.
    """
    track, created = Track.get_or_create(s, defaults=defaults, **key)
    updated = not created and track.fill_nulls(defaults)
    return track, created, updated


# ── Stage 1: source tracks ─────────────────────────────────────────────────────


def _bind_track_files(source_track: SourceTrack, s: Session) -> list[TrackFile]:
    """Find unbound TrackFiles whose on-disk path matches this source track's Apple naming.

    This is the *filename* binding pass (see module docstring): it joins on ``source_path``, not
    on the file's embedded tags, so it still works when those tags diverge from the source.
    """
    # source_path matching is a substring LIKE, so a single rel_path can hit multiple files
    # (same tail under different roots), and overlapping possible_locations can re-hit the same
    # file — hence .all() instead of .one_or_none() (which would raise), keyed by id to dedup.
    # icontains (ILIKE), not contains (LIKE): the path rebuilt from source metadata and the path
    # stored on disk routinely differ only in case, and a case-sensitive match drops those files.
    # autoescape=True keeps '_' and '%' literal — paths sanitize '/' to '_' (e.g. "AC_DC"), and an
    # unescaped '_' is a single-char LIKE wildcard that would silently over-match.
    tfs: dict[int, TrackFile] = {}
    for rel_path in source_track.possible_locations(max_suffix=2):
        stmt = select(TrackFile).where(TrackFile.source_path.icontains(rel_path, autoescape=True))
        for tf in s.scalars(stmt):
            if tf.track_id is None:
                tfs[tf.id] = tf
    return list(tfs.values())


def expects_local_file(st: SourceTrack) -> bool:
    """Whether a local audio file is expected on disk for this source track.

    File binding is attempted for every source track regardless; this helper only encodes the
    "a local copy should exist" expectation (XML tracks not added from Apple Music; MS tracks with
    a known audio extension) so a missing match can be surfaced.
    """
    if st.provider == Source.APPLE_XML:
        return not st.extra.get("apple_music", False)
    return False


def _unify_source_tracks(s: Session) -> Iterator[tuple[bool, bool, int]]:
    """Yield ``(created, updated, n_files_bound)`` per unlinked SourceTrack as it is unified."""
    # Process in insertion order: makes runs reproducible and gives a deterministic "first import wins"
    # for canonical metadata — the earliest-inserted source row creates the Track and sets its defaults,
    # later siblings only backfill NULLs (see _upsert_track).
    stmt = select(SourceTrack).where(SourceTrack.track_id.is_(None)).order_by(SourceTrack.id)
    for st in s.scalars(stmt):
        st: SourceTrack
        defaults = {
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
        track, created, updated = _upsert_track(
            s,
            title=st.title,
            artist=st.artist,
            album=st.album,
            album_artist=st.album_artist,
            defaults=defaults,
        )
        st.track = track

        # Rely on FS discovery for everyone; the flag only tells us whether to complain on a miss.
        # Appends stay idempotent without a membership check here: _bind_track_files dedups within a
        # call and only returns unbound files, and the per-source-track flush below sets track_id so
        # the next source track's guard skips files already claimed.
        tfs = _bind_track_files(st, s)
        for tf in tfs:
            track.files.append(tf)
        # Warn only on a genuine miss. The same physical file usually has two source rows (Apple XML
        # *and* Apple MS) that resolve to one canonical track; whichever is processed first binds the
        # file, leaving none for the second. Checking ``track.files`` — populated by that sibling —
        # avoids a spurious "not found" for a file that is, in fact, already bound to this track.
        if not tfs and not track.files and expects_local_file(st):
            console.print(f"[dim yellow]not found: {st.possible_locations()[0]!r}[/dim yellow]")

        s.flush()
        yield created, updated, len(tfs)


def unify_source_tracks(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int, int]:
    """Create canonical Tracks from SourceTracks and bind matching files. Returns
    ``(created, updated, files_bound)``."""
    created = updated = files_bound = 0
    for was_created, was_updated, n_files in _unify_source_tracks(s):
        created += was_created
        updated += was_updated
        files_bound += n_files
        if progress is not None:
            progress.update(task, advance=1, created=created, updated=updated, files_bound=files_bound)
    return created, updated, files_bound


# ── Stage 2: source playlists ───────────────────────────────────────────────────


@dataclass
class _SourcePlaylist:
    """A source playlist flattened to canonical track_ids, ready for dedup/merge."""

    name: str
    date_modified: AwareDatetime | None
    date_added: AwareDatetime | None
    description: str | None
    platform: Source
    source_id: str
    track_ids: list[int]


def _gather_source_playlists(s: Session) -> list[_SourcePlaylist]:
    """Flatten non-folder SourcePlaylists, resolving members to canonical track_ids."""
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


def unify_source_playlists(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int]:
    """Create deduplicated canonical Playlists from source playlists.

    Processes newest-to-oldest by date_modified; same-name playlists merge (unique tracks
    appended); playlists whose track set duplicates an existing canonical are skipped.
    Returns ``(playlists_created, tracks_linked)``.
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


# ── Stage 3: orphan files ───────────────────────────────────────────────────────


def _unify_orphan_files(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    """Adopt titled TrackFiles that the filename pass left unbound. Returns ``(created, updated)``.

    This is the *tags* binding pass (see module docstring): ``_upsert_track`` matches the file's
    embedded tags to an existing canonical Track when it can, and only creates a new one if not —
    so a file whose tags happen to match a source-built track binds to it rather than duplicating.
    """
    created = updated = 0
    # Insertion order for reproducibility, mirroring the source-track stage.
    stmt = (
        select(TrackFile)
        .where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
        .order_by(TrackFile.id)
    )
    for tf in s.scalars(stmt):
        year = None
        if tf.date:
            with contextlib.suppress(ValueError, IndexError):
                year = int(tf.date[:4])
        defaults = {
            "duration": round(tf.duration) if tf.duration else None,
            "year": year,
        }
        try:
            st = tf.source_path.stat()
            # st_ctime is creation on Windows / inode-change on Linux; mtime is content edit.
            # min() yields the oldest known timestamp for the file across platforms.
            defaults["date_added"] = datetime.fromtimestamp(min(st.st_ctime, st.st_mtime), tz=UTC)
        except OSError:
            pass

        track, was_created, was_updated = _upsert_track(
            s,
            title=tf.title,
            artist=tf.artist,
            album=tf.album,
            album_artist=tf.album_artist,
            defaults=defaults,
        )
        created += was_created
        updated += was_updated

        tf.track = track
        s.flush()
        progress.update(task, advance=1, created=created, updated=updated)

    return created, updated


# ── Orchestration ───────────────────────────────────────────────────────────────


def do_unify(s: Session):
    """Run the three unify stages and print a per-stage summary."""
    track_count = s.scalars(
        select(func.count()).select_from(SourceTrack).where(SourceTrack.track_id.is_(None))
    ).one()
    pl_count = s.scalars(select(func.count()).select_from(SourcePlaylist).where(~SourcePlaylist.folder)).one()
    orphan_count = s.scalars(
        select(func.count())
        .select_from(TrackFile)
        .where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
    ).one()

    with _progress(
        "[green]{task.fields[created]} new[/green]  "
        "[yellow]{task.fields[updated]} updated[/yellow]  "
        "[cyan]{task.fields[files_bound]} files bound[/cyan]"
    ) as progress:
        task = progress.add_task("Tracks", total=track_count, created=0, updated=0, files_bound=0)
        created, updated, files_bound = unify_source_tracks(s, progress, task)

    with _progress(
        "[magenta]{task.fields[pl_created]} playlists[/magenta]  [blue]{task.fields[tr_linked]} linked[/blue]"
    ) as progress:
        task = progress.add_task("Playlists", total=pl_count, pl_created=0, tr_linked=0)
        pl_created, tr_linked = unify_source_playlists(s, progress, task)

    with _progress(
        "[green]{task.fields[created]} new[/green]  [yellow]{task.fields[updated]} updated[/yellow]"
    ) as progress:
        task = progress.add_task("Orphan files", total=orphan_count, created=0, updated=0)
        orphan_created, orphan_updated = _unify_orphan_files(s, progress, task)

    console.print(
        f"  Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]  "
        f"[cyan]{files_bound} files bound[/cyan]\n"
        f"  Playlists: [magenta]{pl_created} new[/magenta]  [blue]{tr_linked} tracks linked[/blue]\n"
        f"  Orphan files: [green]{orphan_created} new[/green]  [yellow]{orphan_updated} updated[/yellow]"
    )
