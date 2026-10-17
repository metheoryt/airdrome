"""Unify: build canonical ``Track`` and ``Playlist`` records from imported source data.

``do_unify`` runs three independent, idempotent stages in order. Each one turns *source*
rows (whatever a given import wrote) into the canonical records the rest of Airdrome uses:

1. **Source tracks** — every ``SourceTrack`` (Apple XML, Apple Media Services, …) becomes
   or joins a canonical ``Track`` keyed on title/artist/album/album_artist; then its on-disk
   copies are bound to it (see "binding files", below).
2. **Source playlists** — every non-folder ``SourcePlaylist`` becomes or merges into a
   canonical ``Playlist``, deduplicated both by name (merge) and by identical track set (skip).
3. **Orphan files** — ``TrackFile`` rows that *no* source claimed (e.g. scanned from disk but
   never imported) go through the identity ladder (``ladder.resolve_file``): recording id →
   fingerprint → tags, a new ``Track`` from tags being the last resort.

Binding files — the subtle part
--------------------------------
A ``TrackFile`` carries both an on-disk ``source_path`` (named by the Apple app / a prior
organize run) and its own embedded tags (``title``/``artist``/…, read by ``enrich``). Those two
can disagree: tags get edited, go stale, or are missing entirely. So files reach their track
through **two complementary passes with different join keys**, in this order:

  1. By **filename** (stage 1, ``_bind_track_files``): ``possible_locations`` rebuilds the Apple
     on-disk path from the *source's* metadata and tail-matches ``source_path`` through an in-memory
     index (``PathTailIndex``). This binds a file to its source track even when the file's own tags
     are messy or absent.
  2. By **the ladder** (stage 3, ``_unify_orphan_files``): whatever the filename pass left unbound
     is resolved by its recording id, then its fingerprint, then its embedded tags.

Pass 1 is not redundant with pass 2: dropping it would re-route tag-divergent files into pass 2,
whose tags rung would mint a *second* canonical track from the tags — leaving one track with the
source data and no file, and a duplicate with the file and no source data. (Fingerprints cannot
rescue those: an Apple source track has no audio to compare.)

"Idempotent" means re-running unify only fills gaps: existing canonical records are reused and
their NULL fields backfilled, never duplicated — except a hashed field of a stored dedup group's
member (``ladder.backfill``). Stage 1 uses ``upsert_track`` for that get-or-create-then-backfill
logic; stage 3's ladder splits the two, backfilling only a track it binds to.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rich.progress import Progress, TaskID, TextColumn
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.console import console, detail, make_progress
from airdrome.enums import BindMethod, Source
from airdrome.library.enrich import fingerprint_files
from airdrome.library.ladder import LadderContext, resolve_file, stored_member_hashes, upsert_track
from airdrome.library.pathindex import PathTailIndex
from airdrome.models import AwareDatetime, Playlist, PlaylistLink, PlaylistTrack, TrackFile


def _progress(summary: str) -> Progress:
    """A stage progress bar: the standard bar plus a per-stage summary column."""
    return make_progress(TextColumn(summary))


# ── Stage 1: source tracks ─────────────────────────────────────────────────────


def _bind_track_files(source_track: SourceTrack, s: Session, index: PathTailIndex) -> list[TrackFile]:
    """Bind unbound TrackFiles whose on-disk path matches this source track's Apple naming.

    This is the *filename* binding pass (see module docstring): it joins on ``source_path``, not
    on the file's embedded tags, so it still works when those tags diverge from the source. The
    path-tail ``index`` was built once per run; each bound file is claimed in it so no second
    source track can take it.
    """
    # One rel_path can hit several files (same tail under different roots) and overlapping
    # possible_locations can re-hit the same file — hence dict keyed by id to dedup.
    # Matching is case-insensitive (the rebuilt path and the stored one routinely differ in case)
    # and on a component boundary; '_' stays literal (paths sanitize '/' to '_', e.g. "AC_DC").
    ids: dict[int, None] = {}
    for rel_path in source_track.possible_locations(max_suffix=2):
        ids.update(dict.fromkeys(index.lookup(rel_path)))
    if not ids:
        return []
    stmt = select(TrackFile).where(TrackFile.id.in_(ids), TrackFile.track_id.is_(None)).order_by(TrackFile.id)
    tfs = list(s.scalars(stmt))
    for tf in tfs:
        # The path is rebuilt from the source track's own metadata, so it outranks tag evidence.
        tf.bind_method = BindMethod.APPLE_PATH
        tf.bind_confidence = 0.9
        index.claim(tf.id)
    return tfs


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
    # later siblings only backfill NULLs (see upsert_track).
    index = PathTailIndex.build(s)
    # Empty on a fresh DB (groups are restored by `dedup`), so a first `land` backfills everything.
    stored_hashes = stored_member_hashes(s)
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
        track, created, updated = upsert_track(
            s,
            title=st.title,
            artist=st.artist,
            album=st.album,
            album_artist=st.album_artist,
            defaults=defaults,
            stored_hashes=stored_hashes,
        )
        st.track = track

        # Rely on FS discovery for everyone; the flag only tells us whether to complain on a miss.
        # Appends stay idempotent without a membership check here: _bind_track_files dedups within a
        # call and only returns unbound files, and the per-source-track flush below sets track_id so
        # the next source track's guard skips files already claimed.
        tfs = _bind_track_files(st, s, index)
        for tf in tfs:
            track.files.append(tf)
        # Warn only on a genuine miss. The same physical file usually has two source rows (Apple XML
        # *and* Apple MS) that resolve to one canonical track; whichever is processed first binds the
        # file, leaving none for the second. Checking ``track.files`` — populated by that sibling —
        # avoids a spurious "not found" for a file that is, in fact, already bound to this track.
        if not tfs and not track.files and expects_local_file(st):
            # Verbose-only: a large XML import misses many files, which would swamp the bar.
            detail(f"[dim yellow]not found: {st.possible_locations()[0]!r}[/dim yellow]")

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


def _append_tracks(s: Session, canonical: Playlist, existing_ids: set[int], track_ids: list[int]) -> int:
    """Append ``track_ids`` not already in ``existing_ids`` after the canonical's last position.

    Mutates ``existing_ids`` in place. Returns the number of tracks linked. Shared by both unify
    modes — the merge accumulator and the per-source no-merge path link tracks the same way.
    """
    max_pos_row = s.scalars(
        select(PlaylistTrack)
        .where(PlaylistTrack.playlist_id == canonical.id)
        .order_by(PlaylistTrack.position.desc())
    ).first()
    next_pos = (max_pos_row.position + 1) if max_pos_row else 1

    linked = 0
    for track_id in track_ids:
        if track_id not in existing_ids:
            s.add(PlaylistTrack(playlist_id=canonical.id, track_id=track_id, position=next_pos))
            existing_ids.add(track_id)
            next_pos += 1
            linked += 1
    return linked


def _unify_per_source(
    s: Session, sources: list[_SourcePlaylist], progress: Progress | None, task: TaskID | None
) -> tuple[int, int]:
    """No-merge mode: each source playlist becomes its own canonical, keyed on (platform, source_id).

    Same-name playlists from different sources coexist. land only **seeds** a playlist's
    membership on first creation; an already-existing canonical is left untouched and its
    updates flow through ``sync`` instead (which reconciles each source against a base). This
    is what stops a re-imported snapshot from resurrecting a track deleted downstream.
    Returns ``(playlists_created, tracks_linked)``.
    """
    playlists_created = tracks_linked = 0
    for src in sources:
        if not src.track_ids:
            if progress is not None:
                progress.update(task, advance=1)
            continue

        canonical, created = Playlist.get_or_create(
            s,
            defaults={
                "name": src.name,
                "description": src.description,
                "date_added": src.date_added,
                "date_modified": src.date_modified,
            },
            platform=src.platform,
            source_id=src.source_id,
        )
        playlists_created += created
        if created:
            # Seed the new playlist; existing ones are owned by `sync`, not re-unioned here.
            tracks_linked += _append_tracks(s, canonical, set(), src.track_ids)

        s.flush()
        if progress is not None:
            progress.update(task, advance=1, pl_created=playlists_created, tr_linked=tracks_linked)

    return playlists_created, tracks_linked


def unify_source_playlists(
    s: Session,
    progress: Progress | None = None,
    task: TaskID | None = None,
    *,
    merge_by_name: bool = False,
) -> tuple[int, int]:
    """Create canonical Playlists from source playlists. Returns ``(playlists_created, tracks_linked)``.

    Default (``merge_by_name=False``): each source playlist becomes its own canonical keyed on
    (platform, source_id); same-name playlists coexist.

    ``merge_by_name=True``: processes newest-to-oldest by date_modified and collapses same-name
    playlists into one canonical (the newest anchors; unique tracks from older ones appended);
    playlists whose track set duplicates an existing canonical are skipped.
    """
    sources = _gather_source_playlists(s)

    if not merge_by_name:
        return _unify_per_source(s, sources, progress, task)

    existing = list(s.scalars(select(Playlist)))
    name_to_canonical: dict[str, Playlist] = {pl.name: pl for pl in existing}
    # Playlists from a prior land are owned by `sync` now — merge only into ones created this run.
    preexisting_ids = {pl.id for pl in existing}

    # Mutable per-canonical track-ID sets; updated in-place as we merge
    canonical_track_ids: dict[int, set[int]] = {
        pl.id: {
            pt.track_id for pt in s.scalars(select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id))
        }
        for pl in existing
    }

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
            if canonical.id not in preexisting_ids:  # only merge into playlists seeded this run
                tracks_linked += _append_tracks(
                    s, canonical, canonical_track_ids[canonical.id], src.track_ids
                )

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


def _unify_orphan_files(s: Session, progress: Progress, task: TaskID) -> LadderContext:
    """Resolve every TrackFile the filename pass left unbound through the identity ladder.

    Returns the run's ``LadderContext``, whose ``stats`` count created/updated tracks, files left
    ``unbound``, and bindings per method. Files lacking a fingerprint get one first, so ``land`` works
    without a prior ``maint enrich``; a file that cannot be fingerprinted still reaches the tags rung.
    """
    # Insertion order for reproducibility, mirroring the source-track stage. No title filter: a file
    # with junk or missing tags can still resolve by recording id or fingerprint.
    orphans = list(s.scalars(select(TrackFile).where(TrackFile.track_id.is_(None)).order_by(TrackFile.id)))
    # A missing file cannot be fingerprinted; skip it rather than spawn fpcalc just to fail.
    unprinted = [tf for tf in orphans if tf.fingerprint is None and tf.absolute_path.is_file()]
    if unprinted:
        console.print(f"  fingerprinting {len(unprinted)} unbound file(s)")
        fingerprint_files(s, unprinted)

    ctx = LadderContext.build(s)
    pending: list[TrackFile] = []
    for tf in orphans:
        if resolve_file(s, tf, ctx) is None:
            pending.append(tf)
        s.flush()
        progress.update(task, advance=1, created=ctx.stats["created"], updated=ctx.stats["updated"])
    # A title-less file resolved before its tagged sibling found nothing in ctx yet; the sibling's
    # binding may now match it (and that binding may enable another). Retry until a pass binds none,
    # so one land binds what a second land would.
    while pending:
        still = [tf for tf in pending if resolve_file(s, tf, ctx) is None]
        s.flush()
        if len(still) == len(pending):
            break
        pending = still
    ctx.stats["unbound"] = len(pending)
    return ctx


# ── Orchestration ───────────────────────────────────────────────────────────────


def _reset_canonical_playlists(s: Session) -> tuple[int, int]:
    """Drop every canonical ``Playlist`` so the playlist stage rebuilds them from source data.

    The playlist stage is append-only (it only fills gaps), so changed merge/dedup logic never
    reshapes playlists that already exist. Wiping them first forces a clean rebuild. Deleting a
    ``Playlist`` cascades (FK ``ON DELETE CASCADE``) to its ``PlaylistTrack`` rows *and* to any
    ``PlaylistLink`` backend-sync snapshots — so this also discards e.g. Navidrome sync state, and
    the rebuilt playlists get new IDs. A backend re-sync afterwards therefore re-pushes from
    scratch. Returns ``(playlists_dropped, links_dropped)``.
    """
    playlists = s.scalars(select(func.count()).select_from(Playlist)).one()
    links = s.scalars(select(func.count()).select_from(PlaylistLink)).one()
    s.execute(delete(Playlist))
    s.flush()
    return playlists, links


def do_unify(s: Session, *, merge_playlists: bool = False, rebuild_playlists: bool = False) -> None:
    """Run the three unify stages and print a per-stage summary.

    ``merge_playlists`` collapses same-name source playlists into one canonical (see
    ``unify_source_playlists``); off by default, so each source playlist stays its own.

    ``rebuild_playlists`` drops all canonical playlists first (see ``_reset_canonical_playlists``)
    so the playlist stage recreates them from scratch — needed because the stage is otherwise
    append-only. Off by default; note it also discards backend-sync links.
    """
    track_count = s.scalars(
        select(func.count()).select_from(SourceTrack).where(SourceTrack.track_id.is_(None))
    ).one()
    pl_count = s.scalars(select(func.count()).select_from(SourcePlaylist).where(~SourcePlaylist.folder)).one()

    with _progress(
        "[green]{task.fields[created]} new[/green]  "
        "[yellow]{task.fields[updated]} updated[/yellow]  "
        "[cyan]{task.fields[files_bound]} files bound[/cyan]"
    ) as progress:
        task = progress.add_task("Tracks", total=track_count, created=0, updated=0, files_bound=0)
        created, updated, files_bound = unify_source_tracks(s, progress, task)

    if rebuild_playlists:
        dropped, links = _reset_canonical_playlists(s)
        console.print(
            f"  [yellow]Rebuild:[/yellow] dropped [magenta]{dropped}[/magenta] canonical "
            f"playlists and [red]{links}[/red] backend link(s)"
        )

    with _progress(
        "[magenta]{task.fields[pl_created]} playlists[/magenta]  [blue]{task.fields[tr_linked]} linked[/blue]"
    ) as progress:
        task = progress.add_task("Playlists", total=pl_count, pl_created=0, tr_linked=0)
        pl_created, tr_linked = unify_source_playlists(s, progress, task, merge_by_name=merge_playlists)

    # Counted after stage 1, which binds many of them; whatever is still unbound enters the ladder.
    orphan_count = s.scalars(
        select(func.count()).select_from(TrackFile).where(TrackFile.track_id.is_(None))
    ).one()
    with _progress(
        "[green]{task.fields[created]} new[/green]  [yellow]{task.fields[updated]} updated[/yellow]"
    ) as progress:
        task = progress.add_task("Orphan files", total=orphan_count, created=0, updated=0)
        ladder = _unify_orphan_files(s, progress, task).stats

    console.print(
        f"  Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]  "
        f"[cyan]{files_bound} files bound[/cyan]\n"
        f"  Playlists: [magenta]{pl_created} new[/magenta]  [blue]{tr_linked} tracks linked[/blue]\n"
        f"  Orphan files: [green]{ladder['created']} new[/green]  "
        f"[yellow]{ladder['updated']} updated[/yellow]  [red]{ladder['unbound']} left unbound[/red]  "
        f"[cyan]by {_method_summary(ladder)}[/cyan]"
    )


def _method_summary(stats: Counter[str]) -> str:
    """``method n`` pairs for the ladder's bindings, in rung order; ``none`` when nothing was bound."""
    parts = [f"{m} {stats[m]}" for m in BindMethod if stats[m]]
    return ", ".join(parts) or "none"
