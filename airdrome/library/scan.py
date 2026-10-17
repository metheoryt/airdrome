"""Scanning music folders: the first-import `MusicScanner`, and `classify` for loose folders.

Spec: ``docs/design/identity-ladder.md`` (*`import <dirs>` for loose files*).
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from mutagen import MutagenError
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import console
from airdrome.enums import BindMethod
from airdrome.library import fingerprint
from airdrome.library.enrich import fingerprint_files
from airdrome.library.fingerprint import FP_MATCH, Fingerprint, FingerprintIndex
from airdrome.library.ladder import FP_MAX_DURATION_GAP, LadderContext, match_file
from airdrome.library.tags import AUDIO_EXTENSIONS, FileTags, file_md5, read_tags
from airdrome.models import ScanCache, Track, TrackFile, TrackGroup


class MusicScanner:
    EXTENSIONS: ClassVar[frozenset[str]] = AUDIO_EXTENSIONS

    def __init__(self, target_path: Path, match_threshold: float = 0.4) -> None:
        self.target_path = target_path

    def scan_file(self, abs_path: Path, s: Session) -> tuple[TrackFile | None, bool]:
        """Enrich ``abs_path``'s TrackFile, creating it if new; ``(None, False)`` for an unreadable file.

        One unreadable file is reported and skipped, like ``classify`` does, instead of aborting the
        scan. A new row joins the session only once it has been read, so nothing half-empty is
        flushed; an existing row's partial in-memory update is discarded.
        """
        tf = s.scalars(select(TrackFile).where(TrackFile.source_path == abs_path)).one_or_none()
        created = tf is None
        if tf is None:
            tf = TrackFile(source_path=abs_path)
        try:
            tf.enrich()
        except (OSError, ValueError, MutagenError) as exc:
            console.print(
                f"  [yellow]skipped[/yellow] {escape(str(abs_path))}: {escape(str(exc) or 'unreadable')}"
            )
            if not created:
                s.expire(tf)
            return None, False
        if created:
            s.add(tf)
        return tf, created

    def scan_all(self, s: Session, _on_item: Callable[[int], None] | None = None) -> int:
        """
        Core scan logic. Returns number of new TrackFile records created.

        Testable directly — no session creation, no progress output.
        """
        n_created = 0
        for abs_path in self.target_path.rglob("*"):
            if not abs_path.is_file() or abs_path.suffix.lower() not in self.EXTENSIONS:
                continue

            _, created = self.scan_file(abs_path, s)
            if created:
                n_created += 1
            s.flush()
            if _on_item:
                _on_item(n_created)

        return n_created

    def run(self, s: Session) -> None:
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("✅ {task.fields[file_created]} new files"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        with progress:
            task_id = progress.add_task(
                f"Scanning {self.target_path}",
                total=None,
                file_created=0,
            )

            def _on_item(n_created: int) -> None:
                progress.update(task_id, advance=1, file_created=n_created)

            self.scan_all(s, _on_item=_on_item)


# ── classify: loose folders against the library ─────────────────────────────────


class Verdict(StrEnum):
    """What importing a recording would add: nothing (`have`), a recording, or a better copy."""

    NEW = "new"
    HAVE = "have"
    UPGRADE = "upgrade"


@dataclass
class Candidate:
    """One recording found in the scanned folders: every copy seen, the best one, and its verdict.

    `in_library` marks a `best` the library already holds (by path or bytes): it is reported like any
    other copy but never imported again. `fp` is None when `fpcalc` failed.
    """

    paths: list[Path]
    best: Path
    tags: FileTags
    fp: Fingerprint | None
    content_hash: str
    verdict: Verdict
    group_canon: Track | None
    in_library: bool = False

    def to_trackfile(self) -> TrackFile:
        """A new, transient TrackFile for `best`, carrying the scan's tags, hash and fingerprint."""
        return _trackfile(self.best, self.tags, self.fp, self.content_hash)


def _trackfile(path: Path, t: FileTags, fp: Fingerprint | None, content_hash: str | None) -> TrackFile:
    """A transient TrackFile for `path` filled from a scan: what `enrich` reads, plus the fingerprint."""
    tf = TrackFile(
        source_path=path,
        title=t.title,
        artist=t.artist,
        album=t.album,
        album_artist=t.album_artist,
        date=t.date,
        duration=t.duration,
        bitrate=t.bitrate,
        acoustid_id=t.acoustid_id,
        content_hash=content_hash,
    )
    if t.recording_mbid:
        tf.recording_mbid = t.recording_mbid
        tf.mbid_source = BindMethod.MBID
    if fp is not None:
        # Stored now so `land` and later classifies never fingerprint this file again.
        tf.fingerprint = fp.raw
        tf.fp_duration = fp.duration
    return tf


def probe(s: Session, path: Path) -> ScanCache:
    """The cached md5, fingerprint and tag recording id of `path`, recomputed when its size or mtime moved.

    Raises what `read_tags` raises for an unreadable file (nothing is cached for it). A failed
    fingerprint is cached as None: the same bytes would fail again.
    """
    return _probe(s, path)[0]


def _probe(s: Session, path: Path) -> tuple[ScanCache, FileTags]:
    """`probe`, also returning the tags: read once on a miss, re-read on a hit (R5 — mutagen is cheap)."""
    st = path.stat()
    row = s.scalars(select(ScanCache).where(ScanCache.path == str(path))).one_or_none()
    if row is not None and row.size == st.st_size and row.mtime == st.st_mtime:
        return row, read_tags(path)
    tags = read_tags(path)
    try:
        fp: Fingerprint | None = fingerprint.compute(path)  # via the module, so tests can count calls
    except fingerprint.FingerprintError:
        fp = None
    if row is None:
        row = ScanCache(path=str(path))
        s.add(row)
    row.size = st.st_size
    row.mtime = st.st_mtime
    row.content_hash = file_md5(path)
    row.fingerprint = fp.raw if fp else None
    row.fp_duration = fp.duration if fp else None
    row.recording_mbid = tags.recording_mbid
    s.flush()
    return row, tags


def library_context(s: Session) -> LadderContext:
    """Fingerprint bound library files that lack a fingerprint, then build the ladder context (R13).

    The context indexes only fingerprinted files, and `land` fingerprints only orphans: without this an
    untagged loose copy of a library track bound by filename would find no rung and read as `new`.
    A missing file is skipped, as in land's orphan stage. Under `-n` the fingerprints roll back too.
    """
    unprinted = [
        tf
        for tf in s.scalars(
            select(TrackFile)
            .where(TrackFile.track_id.is_not(None), TrackFile.fingerprint.is_(None))
            .order_by(TrackFile.id)
        )
        if tf.absolute_path.is_file()
    ]
    if unprinted:
        console.print(f"  fingerprinting {len(unprinted)} library file(s)")
        fingerprint_files(s, unprinted)
    return LadderContext.build(s)


@dataclass
class _Copy:
    """One readable file of a scanned folder."""

    path: Path
    tags: FileTags
    cache: ScanCache

    @property
    def fp(self) -> Fingerprint | None:
        """The cached fingerprint, if `fpcalc` produced one."""
        c = self.cache
        return Fingerprint(c.fingerprint, c.fp_duration) if c.fingerprint and c.fp_duration else None


@dataclass
class _Recording:
    """Copies collapsed into one recording: by identical bytes, then by matching fingerprint."""

    copies: list[_Copy] = field(default_factory=list)

    @property
    def recording_mbid(self) -> str | None:
        """The tag recording id of the first copy that has one."""
        return next((c.tags.recording_mbid for c in self.copies if c.tags.recording_mbid), None)


def _walk(roots: list[Path]) -> Iterator[Path]:
    """Every audio file under `roots`, each once, in a stable order, as an absolute path."""
    seen: set[Path] = set()
    for root in roots:
        # Resolved, so a relative CLI argument still yields the absolute `source_path` the model expects.
        for p in sorted(root.resolve().rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and p not in seen:
                seen.add(p)
                yield p


def _read(s: Session, roots: list[Path]) -> list[_Copy]:
    """Probe and tag-read every audio file under `roots`; an unreadable file is reported and skipped."""
    copies = []
    for path in _walk(roots):
        try:
            cache, tags = _probe(s, path)
        except (OSError, ValueError, MutagenError) as exc:
            console.print(
                f"  [yellow]skipped[/yellow] {escape(str(path))}: {escape(str(exc) or 'unreadable')}"
            )
            continue
        copies.append(_Copy(path, tags, cache))
    return copies


def _collapse(copies: list[_Copy]) -> list[_Recording]:
    """Collapse repeats: identical bytes first, then identical audio (fingerprint and duration gate).

    Two copies whose tag recording ids disagree never collapse — an id outranks a fingerprint, as in
    the ladder.
    """
    by_hash: dict[str, _Recording] = {}
    for c in copies:
        by_hash.setdefault(c.cache.content_hash or str(c.path), _Recording()).copies.append(c)

    recordings: list[_Recording] = []
    index = FingerprintIndex()  # keyed by position in `recordings`
    for rec in by_hash.values():
        fp = rec.copies[0].fp
        target = None
        if fp is not None:
            for key, _score in index.matches(fp.raw, threshold=FP_MATCH):
                other = recordings[key]
                other_fp = other.copies[0].fp
                ids = {rec.recording_mbid, other.recording_mbid} - {None}
                if other_fp and abs(other_fp.duration - fp.duration) <= FP_MAX_DURATION_GAP and len(ids) < 2:
                    target = other
                    break
        if target is not None:
            target.copies.extend(rec.copies)
            continue
        if fp is not None:
            index.add(len(recordings), fp.raw)
        recordings.append(rec)
    return recordings


def _draft(c: _Copy) -> TrackFile:
    """A transient TrackFile for one copy, enough for `select_main_file` and `match_file`."""
    return _trackfile(c.path, c.tags, c.fp, c.cache.content_hash)


def _kbps(tf: TrackFile) -> int:
    """`tf`'s bitrate in kbps, the unit `select_main_file` compares; unknown counts as 0."""
    return (tf.bitrate or 0) // 1000


def _verdict(canon: Track | None, best: TrackFile) -> Verdict:
    """`new` with no group; else `upgrade` when `best` beats the group's best file, `have` otherwise.

    The group's *best* file, not its `is_main` one: an upgrade imported but not yet organized is not
    main, and comparing against main would call it an upgrade again on every run. A group with no file
    at all (a cloud-only track) gets one: that is an upgrade over nothing.
    """
    if canon is None:
        return Verdict.NEW
    files = [f for m in TrackGroup.of(canon).members for f in m.files]
    if not files:
        return Verdict.UPGRADE
    return Verdict.UPGRADE if _kbps(best) > max(_kbps(f) for f in files) else Verdict.HAVE


# Copies per lookup query: two bind parameters each, well under Postgres's 65535-parameter cap.
_LOOKUP_CHUNK = 5000


def _library_files(s: Session, copies: list[_Copy]) -> dict[Path | str, TrackFile]:
    """Library files these copies already are, keyed by source path and by md5; not the whole table."""
    known: dict[Path | str, TrackFile] = {}
    for i in range(0, len(copies), _LOOKUP_CHUNK):
        chunk = copies[i : i + _LOOKUP_CHUNK]
        paths = [c.path for c in chunk]
        hashes = [c.cache.content_hash for c in chunk if c.cache.content_hash]
        stmt = select(TrackFile).where(TrackFile.source_path.in_(paths) | TrackFile.content_hash.in_(hashes))
        for tf in s.scalars(stmt):
            known[tf.source_path] = tf
            if tf.content_hash:
                known.setdefault(tf.content_hash, tf)
    return known


def classify(s: Session, roots: list[Path], ctx: LadderContext | None = None) -> list[Candidate]:
    """Report every recording under `roots` as new / have / upgrade against the library.

    Writes only `ScanCache` rows. Copies the library already holds (a `TrackFile.source_path`, or a
    `TrackFile` with the same bytes) are not skipped from the report: their recording is classified
    from the TrackFile's own group, so a re-run over the same folders lists the same recordings, now
    as `have`. Pass `ctx` to reuse it for importing afterwards.
    """
    ctx = ctx or library_context(s)
    copies = _read(s, roots)
    known = _library_files(s, copies)

    def held(c: _Copy) -> TrackFile | None:
        """The library file that is this copy, by path or by bytes."""
        return known.get(c.path) or known.get(c.cache.content_hash or "")

    candidates = []
    for rec in _collapse(copies):
        drafts = [(c, _draft(c)) for c in rec.copies]
        best = TrackGroup.select_main_file([d for _, d in drafts])
        best_copy = next(c for c, d in drafts if d is best)
        bound = next((tf.track for c in rec.copies if (tf := held(c)) and tf.track is not None), None)
        canon = (bound.canon or bound) if bound else match_file(s, best, ctx)
        candidates.append(
            Candidate(
                paths=[c.path for c in rec.copies],
                best=best_copy.path,
                tags=best_copy.tags,
                fp=best_copy.fp,
                content_hash=best_copy.cache.content_hash or "",
                verdict=_verdict(canon, best),
                group_canon=canon,
                in_library=held(best_copy) is not None,
            )
        )
    return candidates
