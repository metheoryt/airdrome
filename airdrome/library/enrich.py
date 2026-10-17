"""Re-enrich existing `TrackFile` rows: tags, ids, md5 and chromaprint fingerprint."""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.library import fingerprint
from airdrome.library.tags import file_md5
from airdrome.models import TrackFile


@dataclass
class EnrichStats:
    """Counters for one `reenrich` pass; `with_*` are coverage over the files seen."""

    seen: int = 0
    updated: int = 0
    missing: int = 0  # file absent or unreadable (corrupt tags/container)
    fp_failed: int = 0
    with_mbid: int = 0
    with_fp: int = 0


def _compute_or_none(path: Path) -> fingerprint.Fingerprint | None:
    """Fingerprint one path (runs in a worker thread, so it touches no ORM state); failure yields None."""
    try:
        return fingerprint.compute(path)
    except fingerprint.FingerprintError:
        return None


def fingerprint_files(s: Session, files: list[TrackFile], workers: int = 4) -> int:
    """Fingerprint `files` in parallel and store the result; return how many succeeded.

    Only the `fpcalc` subprocess runs in the pool threads; ORM attributes are written on the
    calling thread, since a Session is not thread-safe. Failures are counted, never raised.
    """
    done = 0
    # Paths are read on the calling thread: absolute_path is an ORM attribute and a lazy load
    # from a pool thread would use the Session concurrently.
    paths = [f.absolute_path for f in files]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for file, fp in zip(files, pool.map(_compute_or_none, paths), strict=True):
            if fp is None:
                continue
            file.fingerprint = fp.raw
            file.fp_duration = fp.duration
            done += 1
    s.flush()
    return done


def _refresh(file: TrackFile, force: bool) -> bool | None:
    """Re-read tags/ids/md5 of `file`; True if refreshed, False if skipped, None if missing/unreadable."""
    path = file.absolute_path
    if not path.is_file():
        return None
    old_hash = file.content_hash
    try:
        # The md5 is the change detector, so it is read on every run — but only when there is a
        # stored hash and fingerprint to compare against; otherwise enrich() computes it anyway.
        if (
            not force
            and file.content_hash
            and file.fingerprint is not None
            and file_md5(path) == file.content_hash
        ):
            return False
        file.enrich()
    except OSError, ValueError, MutagenError:
        return None
    if file.content_hash != old_hash:
        # New bytes make the stored fingerprint stale. Unchanged bytes (--force) keep it, so a
        # transient fpcalc failure cannot destroy a good one.
        file.fingerprint = None
        file.fp_duration = None
    return True


def reenrich(s: Session, *, force: bool = False, workers: int = 4) -> EnrichStats:
    """Refresh evidence for every `TrackFile` from `absolute_path`; never touches `track_id`."""
    stats = EnrichStats()
    files: Iterable[TrackFile] = s.scalars(select(TrackFile).order_by(TrackFile.id)).all()
    to_fingerprint: list[TrackFile] = []
    alive: list[TrackFile] = []
    for file in files:
        stats.seen += 1
        outcome = _refresh(file, force)
        if outcome is None:
            stats.missing += 1
            continue
        alive.append(file)
        if outcome:
            stats.updated += 1
            to_fingerprint.append(file)
        stats.with_mbid += file.recording_mbid is not None
    s.flush()
    ok = fingerprint_files(s, to_fingerprint, workers)
    stats.fp_failed = len(to_fingerprint) - ok
    # Counted after fingerprinting so a refreshed file is neither missed nor counted twice.
    stats.with_fp = sum(f.fingerprint is not None for f in alive)
    return stats
