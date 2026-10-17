"""Read embedded tags and identity ids (MusicBrainz recording id, AcoustID id) from audio files."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mutagen import File


AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma"})

# Per field: the tag key in each container, tried in order (ID3, MP4, Vorbis/FLAC/Opus, ASF).
_KEYS: dict[str, tuple[str, ...]] = {
    "title": ("TIT2", "©nam", "title", "Title"),
    "artist": ("TPE1", "©ART", "artist", "Author"),
    "album": ("TALB", "©alb", "album", "WM/AlbumTitle"),
    "album_artist": ("TPE2", "aART", "albumartist", "WM/AlbumArtist"),
    "date": ("TDRC", "TDOR", "©day", "date", "WM/Year"),
    "recording_mbid": (
        "UFID:http://musicbrainz.org",
        "----:com.apple.iTunes:MusicBrainz Track Id",
        "musicbrainz_trackid",
        "MusicBrainz/Track Id",
    ),
    "acoustid_id": (
        "TXXX:Acoustid Id",
        "----:com.apple.iTunes:Acoustid Id",
        "acoustid_id",
        "Acoustid/Id",
    ),
}

_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FileTags:
    """Tags and stream info of one audio file; `bitrate` is in bps as mutagen reports it."""

    title: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    date: str | None
    recording_mbid: str | None
    acoustid_id: str | None
    duration: float | None
    bitrate: int


def _text(v: object) -> str:
    """Render one tag value as text; bytes (MP4 freeform, UFID) are decoded, never `str()`-ed."""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    data = getattr(v, "data", None)  # ID3 UFID frame keeps its payload in `.data`
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(v)


def extract_tags(tags: Mapping[str, object]) -> dict[str, str | None]:
    """Map a container's raw tag mapping onto the field names of `_KEYS` (None when absent)."""
    out: dict[str, str | None] = {}
    for field, keys in _KEYS.items():
        out[field] = None
        for k in keys:
            if k in tags:
                v = tags[k]
                out[field] = "; ".join(_text(x) for x in v) if isinstance(v, list) else _text(v)
                break
    # An id tag that is present but empty carries no id.
    for field in ("recording_mbid", "acoustid_id"):
        out[field] = out[field] or None
    return out


def read_tags(path: Path) -> FileTags:
    """Open `path` with mutagen and return its tags and stream info; raises ValueError if unreadable."""
    audio = File(path)
    if audio is None:
        raise ValueError("Unsupported or corrupted file")
    fields = extract_tags(audio.tags or {})
    return FileTags(
        **fields,
        duration=audio.info.length,
        bitrate=getattr(audio.info, "bitrate", 0),
    )


def file_md5(path: Path) -> str:
    """Return the md5 hex digest of the whole file, streamed in 1 MiB chunks."""
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()
