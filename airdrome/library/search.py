"""Search the owner's music by words: the library's tracks and the music folders on disk.

Answers "do I have this anywhere?". The DB side lists canon groups whose member matches, with their files.
The disk side walks `settings.search_roots` and lists matching folders and audio files, each file marked
as imported (bound to a track) or not. A folder holding no audio — only cover art left behind — is the
trace of a file that was there once.

Matching is by whole words, in any order: every query word must equal a word of the target, except the
last, which may be a prefix (so a half-typed word still finds). Substrings would not do: `a-ha`
normalizes to `aha`, which is inside `baha`, and the query `me` would hit every path under `/home/me`.
Paths are matched relative to their search root for the same reason.
"""

import os
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourceTrack
from airdrome.conf import settings
from airdrome.library.tags import AUDIO_EXTENSIONS
from airdrome.models import Track, TrackFile, TrackPlay
from airdrome.normalize.norm import normalize_name
from airdrome.wanted.lists import LIBRARY_PROVIDERS
from airdrome.wanted.paging import MAX_LIMIT
from airdrome.wanted.verdicts import group_verdicts


# Subfolders without audio listed per folder hit; a whole artist tree can have many.
NO_AUDIO_CAP = 20
_WORD = re.compile(r"\w+")


# Cached: normalizing is the bulk of a search's time, and the server sees the same names on every call.
@lru_cache(maxsize=200_000)  # ~16k tracks x 4 fields plus ~30k path names, a few MB
def words(text: str | None) -> tuple[str, ...]:
    """`text` as normalized words; `_` counts as a space (Picard writes `:` and `/` in names as `_`)."""
    return tuple(_WORD.findall(normalize_name((text or "").replace("_", " "))))


def matches(query: tuple[str, ...], target: tuple[str, ...] | set[str]) -> bool:
    """Whether every query word is a word of `target`, the last one possibly as a prefix."""
    *whole, last = query
    pool = set(target)
    return all(w in pool for w in whole) and any(t.startswith(last) for t in pool)


def search(s: Session, query: str, limit: int = 20) -> dict:
    """Tracks and disk entries matching `query`, each section capped at `limit` with its total."""
    q = words(query)
    if not q:
        raise ValueError("the query has no words to search for")
    limit = max(1, min(limit, MAX_LIMIT))
    return {"tracks": search_tracks(s, q, limit), "disk": search_disk(s, q, limit)}


def search_tracks(s: Session, q: tuple[str, ...], limit: int) -> dict:
    """Canon groups with a member whose title, artist, album or album artist words match `q`."""
    rows = s.execute(
        select(
            Track.id,
            func.coalesce(Track.canon_id, Track.id),
            Track.title_norm,
            Track.artist_norm,
            Track.album_norm,
            Track.album_artist_norm,
        )
    ).all()
    # Matched in Python: ~16k short rows, and word matching has no index to use anyway.
    hits: dict[int, list[int]] = defaultdict(list)
    for tid, root, *fields in rows:
        if matches(q, {w for f in fields for w in words(f)}):
            hits[root].append(tid)
    if not hits:
        return {"total": 0, "rows": []}

    roots = list(hits)
    member_root = {tid: root for tid, root, *_ in rows}
    group_ids = [tid for tid, root in member_root.items() if root in hits]
    plays: dict[int, int] = defaultdict(int)
    for tid, n in s.execute(
        select(TrackPlay.track_id, func.count())
        .where(TrackPlay.track_id.in_(group_ids))
        .group_by(TrackPlay.track_id)
    ):
        plays[member_root[tid]] += n
    # Most played first: the group the owner means is usually the one they listen to.
    roots.sort(key=lambda r: (-plays[r], r))
    page = roots[:limit]

    page_ids = [tid for tid in group_ids if member_root[tid] in page]
    tracks = {t.id: t for t in s.scalars(select(Track).where(Track.id.in_(page)))}
    files: dict[int, list[dict]] = defaultdict(list)
    for f in s.scalars(select(TrackFile).where(TrackFile.track_id.in_(page_ids)).order_by(TrackFile.id)):
        files[member_root[f.track_id]].append(
            {"track_id": f.track_id, "path": str(f.absolute_path), "is_main": f.is_main, "bitrate": f.bitrate}
        )
    in_library = set(
        s.scalars(
            select(func.coalesce(Track.canon_id, Track.id))
            .join(SourceTrack, SourceTrack.track_id == Track.id)
            .where(Track.id.in_(page_ids), SourceTrack.provider.in_(LIBRARY_PROVIDERS))
        )
    )
    verdicts = group_verdicts(s)

    out = []
    for r in page:
        canon = tracks[r]
        verdict = verdicts.get(r)
        out.append(
            {
                "track_id": r,
                "title": canon.title,
                "artist": canon.artist,
                "album": canon.album,
                "members": sum(1 for root in member_root.values() if root == r),
                "matched_member_ids": sorted(hits[r]),
                "plays": plays[r],
                "in_library": r in in_library,
                "has_file": bool(files[r]),
                "files": files[r],
                "verdict": verdict.value if verdict else None,
            }
        )
    return {"total": len(roots), "rows": out}


def _roots() -> list[Path]:
    """The search roots that exist, minus any root inside another (it would be walked twice).

    Roots are walked as configured, not resolved, so walked paths compare equal to the stored file paths.
    """
    roots = list(dict.fromkeys(p for p in settings.search_roots if p.is_dir()))
    real = {r: r.resolve() for r in roots}
    return [r for r in roots if not any(o != r and real[r].is_relative_to(real[o]) for o in roots)]


def _known_files(s: Session) -> dict[str, int]:
    """Absolute path of every bound file, its source and its library copy, to the file's track id."""
    known: dict[str, int] = {}
    for src, lib, tid in s.execute(select(TrackFile.source_path, TrackFile.library_path, TrackFile.track_id)):
        known[str(src)] = tid
        if lib is not None:
            known[str(settings.library_dir / lib)] = tid
    return known


def _is_audio(name: str) -> bool:
    """Whether a file name has an audio extension."""
    return os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS


def _folder_hit(top: Path, known: dict[str, int]) -> dict:
    """A matching folder: its audio counts over the whole subtree and the subfolders holding no audio."""
    audio = imported = 0
    has_audio: dict[str, bool] = {}
    # Bottom-up, so a folder knows whether any folder below it holds audio.
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        here = [f for f in filenames if _is_audio(f)]
        audio += len(here)
        imported += sum(1 for f in here if os.path.join(dirpath, f) in known)
        has_audio[dirpath] = bool(here) or any(
            has_audio.get(os.path.join(dirpath, d), False) for d in dirnames
        )
    # Topmost only: everything below a folder without audio is without audio too, so it adds nothing.
    empty = sorted(
        os.path.relpath(d, top)
        for d, ok in has_audio.items()
        if not ok and (d == str(top) or has_audio[os.path.dirname(d)])
    )
    return {
        "kind": "folder",
        "path": str(top),
        "audio_files": audio,
        "imported": imported,
        "folders_without_audio": empty[:NO_AUDIO_CAP],
        "folders_without_audio_total": len(empty),
    }


def search_disk(s: Session, q: tuple[str, ...], limit: int) -> dict:
    """Folders and audio files under the search roots whose path below the root matches `q`.

    A matching folder is reported once, with its whole subtree folded in, and is not descended into; an
    audio file is reported on its own only when no folder above it matched.
    """
    roots = _roots()
    known = _known_files(s)
    hits: list[dict] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            rel = os.path.relpath(dirpath, root)
            base = () if rel == "." else words(rel)
            if base and matches(q, base):
                hits.append(_folder_hit(Path(dirpath), known))
                dirnames[:] = []
                continue
            for f in sorted(filenames):
                if _is_audio(f) and matches(q, base + words(os.path.splitext(f)[0])):
                    path = os.path.join(dirpath, f)
                    hits.append({"kind": "file", "path": path, "track_id": known.get(path)})
    hits.sort(key=lambda h: h["path"])
    return {"roots": [str(r) for r in roots], "total": len(hits), "rows": hits[:limit]}
