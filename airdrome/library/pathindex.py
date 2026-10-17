"""In-memory index of unbound ``TrackFile`` paths, keyed by file name, for Apple path binding."""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.models import TrackFile


class PathTailIndex:
    """Looks up unbound files whose path ends with a given relative path, case-insensitively.

    Replaces one ILIKE query per source track: the unbound files are loaded once and bucketed by
    their last path component, so a lookup only scans the few files sharing that file name. All
    characters are literal (``_`` and ``%`` carry no wildcard meaning here).
    """

    def __init__(self, files: list[tuple[int, str]]) -> None:
        """Bucket ``(id, posix_path)`` pairs by lower-cased file name."""
        self._buckets: dict[str, list[tuple[int, str]]] = defaultdict(list)
        self._name_of: dict[int, str] = {}
        for file_id, path in files:
            lowered = path.lower()
            name = lowered.rsplit("/", 1)[-1]
            self._buckets[name].append((file_id, lowered))
            self._name_of[file_id] = name

    @classmethod
    def build(cls, s: Session) -> PathTailIndex:
        """Load ``(id, source_path)`` of every unbound file once, in id order."""
        stmt = select(TrackFile.id, TrackFile.source_path).where(TrackFile.track_id.is_(None))
        return cls([(fid, path.as_posix()) for fid, path in s.execute(stmt.order_by(TrackFile.id))])

    def lookup(self, rel_path: str) -> list[int]:
        """Ids of files whose path equals ``rel_path`` or ends with ``"/" + rel_path`` (any case)."""
        rel = rel_path.lower()
        tail = "/" + rel
        bucket = self._buckets.get(rel.rsplit("/", 1)[-1], ())
        # The "/" in `tail` forces the match onto a path-component boundary ("xAC_DC/..." is no hit).
        return [fid for fid, path in bucket if path == rel or path.endswith(tail)]

    def claim(self, file_id: int) -> None:
        """Remove a bound file so later lookups no longer return it."""
        name = self._name_of.pop(file_id, None)
        if name is not None:
            self._buckets[name] = [e for e in self._buckets[name] if e[0] != file_id]
