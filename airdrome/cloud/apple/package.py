import io
import json
import zipfile
from pathlib import Path


class AppleMediaServicesPackage:
    """
    Provides access to Apple Music Activity files from either
    an Apple Media Services .zip export or an extracted directory.

    The zip layout from Apple is unpredictable across exports (nested zips,
    varying directory depths). Files are located by recursively searching
    through any level of zip nesting.
    """

    _TRACKS_FILE = "Apple Music Library Tracks.json.zip"
    _PLAYLISTS_FILE = "Apple Music Library Playlists.json.zip"
    _PLAY_ACTIVITY_FILE = "Apple Music Play Activity.csv"

    def __init__(self, path: Path):
        self._path = path

    # ------------------------------------------------------------------ zip

    def _read_from_zip(self, zf: zipfile.ZipFile, filename: str) -> bytes:
        """Recursively search for *filename* inside *zf*, descending into nested zips."""
        # Direct match — with or without a directory prefix
        for entry in zf.namelist():
            if entry == filename or entry.endswith(f"/{filename}"):
                return zf.read(entry)

        # Recurse into nested zip entries
        for entry in zf.namelist():
            if not entry.endswith(".zip"):
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(entry))) as nested:
                    return self._read_from_zip(nested, filename)
            except FileNotFoundError, zipfile.BadZipFile:
                continue

        raise FileNotFoundError(f"'{filename}' not found inside {self._path.name}")

    # ------------------------------------------------------------------ directory

    def _resolve_activity_dir(self) -> Path:
        """Walk the extracted tree (up to 3 levels) to find the folder with the activity files."""

        def _walk(p: Path, depth: int) -> Path | None:
            if not p.is_dir():
                return None
            if (p / self._TRACKS_FILE).exists():
                return p
            if depth == 0:
                return None
            for child in sorted(p.iterdir()):
                result = _walk(child, depth - 1)
                if result:
                    return result
            return None

        result = _walk(self._path, depth=3)
        if result is None:
            raise FileNotFoundError(f"Apple Music Activity files not found under {self._path}")
        return result

    # ------------------------------------------------------------------ internal

    def _read_activity_file(self, filename: str) -> bytes:
        if self._path.is_file():
            with zipfile.ZipFile(self._path) as z:
                return self._read_from_zip(z, filename)
        act = self._resolve_activity_dir()
        with open(act / filename, "rb") as f:
            return f.read()

    @staticmethod
    def _parse_json_zip(data: bytes) -> list:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with z.open(z.namelist()[0]) as f:
                return json.load(f)

    # ------------------------------------------------------------------ public

    def load_tracks(self) -> list:
        return self._parse_json_zip(self._read_activity_file(self._TRACKS_FILE))

    def load_playlists(self) -> list:
        return self._parse_json_zip(self._read_activity_file(self._PLAYLISTS_FILE))

    def play_activity_text(self) -> io.StringIO:
        return io.StringIO(self._read_activity_file(self._PLAY_ACTIVITY_FILE).decode("utf-8"))
