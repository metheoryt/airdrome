from abc import ABC, abstractmethod
from enum import Flag, auto
from pathlib import Path
from typing import ClassVar

from sqlalchemy.orm import Session


class DataKind(Flag):
    TRACKS = auto()
    PLAYLISTS = auto()
    SCROBBLES = auto()


class Importer(ABC):
    """One concrete ingestion source (an export file/dir or a music folder).

    A subclass declares the kinds it `provides` and implements the matching
    `import_*` hooks; the unimplemented ones stay no-ops. `ingest` runs the
    requested-and-supported kinds in dependency order: tracks → playlists →
    scrobbles (playlist membership and alias matching both lean on tracks).

    `detect` must recognize a path by sniffing its content, not just the
    extension — several formats share an extension or a zip/dir shape.
    """

    name: ClassVar[str]
    """Identifier used for the `--as` override and in registry lookups."""
    label: ClassVar[str]
    """Human-friendly source name shown in import output (e.g. 'Apple iTunes XML')."""
    provides: ClassVar[DataKind]

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool: ...

    def import_tracks(self, s: Session) -> None: ...
    def import_playlists(self, s: Session) -> None: ...
    def import_scrobbles(self, s: Session) -> None: ...

    def ingest(self, s: Session, kinds: DataKind) -> None:
        kinds &= self.provides
        if kinds & DataKind.TRACKS:
            self.import_tracks(s)
        if kinds & DataKind.PLAYLISTS:
            self.import_playlists(s)
        if kinds & DataKind.SCROBBLES:
            self.import_scrobbles(s)
