from pathlib import Path

from .base import Importer
from .sources import (
    AppleMsImporter,
    AppleXmlImporter,
    LastFmImporter,
    ListenBrainzImporter,
    MusicFolderImporter,
    SpotifyImporter,
)


# Most-specific first; the music-folder catch-all must stay last.
IMPORTERS: list[type[Importer]] = [
    AppleXmlImporter,
    AppleMsImporter,
    ListenBrainzImporter,
    SpotifyImporter,
    LastFmImporter,
    MusicFolderImporter,
]

BY_NAME: dict[str, type[Importer]] = {cls.name: cls for cls in IMPORTERS}


def detect(path: Path) -> list[type[Importer]]:
    """Importers that recognize `path`. 0 → unrecognized, >1 → ambiguous (caller decides)."""
    return [cls for cls in IMPORTERS if cls.detect(path)]
