import csv
import io
import json
import plistlib
import zipfile
from collections.abc import Iterable
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from airdrome.cloud.apple.media_services import import_ms_playlist, import_ms_track
from airdrome.cloud.apple.package import AppleMediaServicesPackage
from airdrome.cloud.apple.scrobbles import AppleScrobbleParser
from airdrome.cloud.apple.xml_library import do_import_playlists, do_import_tracks
from airdrome.cloud.lastfm import LastFMScrobbleParser
from airdrome.cloud.listenbrainz import ListenBrainzScrobbleParser
from airdrome.cloud.spotify import SpotifyScrobbleParser
from airdrome.console import console, make_import_progress, make_progress
from airdrome.library.ladder import resolve_file
from airdrome.library.scan import Candidate, MusicScanner, Verdict, classify, library_context
from airdrome.library.tags import AUDIO_EXTENSIONS
from airdrome.models import TrackFile
from airdrome.scrobbles.parser import ScrobbleParser

from .base import DataKind, Importer


_MS_SIGNATURES = ("Apple Music Library Tracks", "Apple Music Play Activity")


def _done(kind: str, detail: str) -> None:
    """Persistent one-line summary printed after a phase's (transient) progress bar."""
    console.print(f"  [bold green]{kind:<9}[/bold green] {detail}")


def _member_names(path: Path, limit: int = 10_000) -> list[str]:
    """Inner entry names for a zip, or shallow file names for a directory tree."""
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    if path.is_dir():
        names = []
        for i, p in enumerate(path.rglob("*")):
            if i >= limit:
                break
            names.append(p.name)
        return names
    return []


def _zip_contains_signature(zf: zipfile.ZipFile, signatures: Iterable[str], depth: int) -> bool:
    """Whether any entry name contains a signature, descending into nested zips.

    Apple's Media Services export buries the activity files inside a nested
    `Apple_Media_Services.zip` (the layout/depth varies between exports), so a
    shallow namelist check misses them. Matches short-circuit before leaf zips
    are read.
    """
    if any(sig in name for name in zf.namelist() for sig in signatures):
        return True
    if depth <= 0:
        return False
    for name in zf.namelist():
        if not name.lower().endswith(".zip"):
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(zf.read(name))) as nested:
                if _zip_contains_signature(nested, signatures, depth - 1):
                    return True
        except zipfile.BadZipFile, OSError, KeyError:
            continue
    return False


def _first_json_records(path: Path) -> list | None:
    """Load the first `.json` array found (in a dir or zip). Returns None if none/unreadable."""
    try:
        if path.is_dir():
            for jf in sorted(path.rglob("*.json")):
                with open(jf, encoding="utf-8") as f:
                    return json.load(f)
        elif path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                for entry in sorted(z.namelist()):
                    if entry.endswith(".json"):
                        with z.open(entry) as f:
                            return json.load(f)
    except OSError, ValueError:
        return None
    return None


class _ScrobbleImporter(Importer):
    """Shared base for sources that only carry play history; delegates to a ScrobbleParser."""

    provides = DataKind.SCROBBLES
    parser_cls: ClassVar[type[ScrobbleParser]]

    def import_scrobbles(self, s: Session) -> None:
        stats = self.parser_cls(self.path).import_aliases_scrobbles(s)
        _done(
            "Scrobbles",
            f"[green]{stats.scrobbles_created}[/green] new "
            f"[dim]({stats.scrobbles_ignored} already known)[/dim]",
        )
        console.print(
            f"    [dim]aliases: {stats.aliases_created} new, "
            f"{stats.aliases_ignored} matched, {stats.aliases_skipped} skipped[/dim]"
        )


class AppleXmlImporter(Importer):
    name = "apple_xml"
    label = "Apple iTunes XML"
    provides = DataKind.TRACKS | DataKind.PLAYLISTS

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() != ".xml":
            return False
        try:
            with open(path, "rb") as f:
                head = f.read(512).lstrip().lower()
        except OSError:
            return False
        return head.startswith(b"<?xml") and b"plist" in head

    @cached_property
    def _plist(self) -> dict:
        with open(self.path, "rb") as f:
            return plistlib.load(f)

    def import_tracks(self, s: Session) -> None:
        tracks_data = self._plist["Tracks"]
        with make_import_progress(transient=True) as progress:
            task = progress.add_task("Tracks", total=len(tracks_data), created=0, updated=0)
            created = do_import_tracks(s, tracks_data, progress=progress, task_id=task)
        _done("Tracks", f"[green]{created}[/green] new")

    def import_playlists(self, s: Session) -> None:
        playlists_data = self._plist["Playlists"]
        with make_progress(transient=True) as progress:
            task = progress.add_task("Playlists", total=len(playlists_data))
            created = do_import_playlists(s, playlists_data, progress=progress, task_id=task)
        _done("Playlists", f"[green]{created}[/green] new")


class AppleMsImporter(_ScrobbleImporter):
    name = "apple_ms"
    label = "Apple Media Services"
    provides = DataKind.TRACKS | DataKind.PLAYLISTS | DataKind.SCROBBLES
    parser_cls = AppleScrobbleParser

    @classmethod
    def detect(cls, path: Path) -> bool:
        if path.is_dir():
            return any(sig in p.name for p in path.rglob("*") for sig in _MS_SIGNATURES)
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return _zip_contains_signature(z, _MS_SIGNATURES, depth=2)
        return False

    @cached_property
    def _package(self) -> AppleMediaServicesPackage:
        return AppleMediaServicesPackage(self.path)

    def import_tracks(self, s: Session) -> None:
        items = self._package.load_tracks()
        created = 0
        with make_import_progress(transient=True) as progress:
            task = progress.add_task("Tracks", total=len(items), created=0, updated=0)
            for item in items:
                if import_ms_track(s, item):
                    created += 1
                    if created % 100 == 0:
                        s.flush()
                progress.update(task, advance=1, created=created)
        s.flush()
        _done("Tracks", f"[green]{created}[/green] new")

    def import_playlists(self, s: Session) -> None:
        items = self._package.load_playlists()
        created = 0
        with make_progress(transient=True) as progress:
            task = progress.add_task("Playlists", total=len(items))
            for pl in items:
                if import_ms_playlist(s, pl):
                    created += 1
                progress.advance(task)
        _done("Playlists", f"[green]{created}[/green] new")


class SpotifyImporter(_ScrobbleImporter):
    name = "spotify"
    label = "Spotify"
    parser_cls = SpotifyScrobbleParser

    @classmethod
    def detect(cls, path: Path) -> bool:
        records = _first_json_records(path)
        return (
            isinstance(records, list)
            and bool(records)
            and isinstance(records[0], dict)
            and "master_metadata_track_name" in records[0]
        )


class ListenBrainzImporter(_ScrobbleImporter):
    name = "listenbrainz"
    label = "ListenBrainz"
    parser_cls = ListenBrainzScrobbleParser

    @classmethod
    def detect(cls, path: Path) -> bool:
        return any(n.endswith(".jsonl") for n in _member_names(path))


class LastFmImporter(_ScrobbleImporter):
    name = "lastfm"
    label = "Last.fm"
    parser_cls = LastFMScrobbleParser

    @classmethod
    def detect(cls, path: Path) -> bool:
        # Last.fm CSV export: headerless artist,album,track,date rows.
        if not path.is_file() or path.suffix.lower() != ".csv":
            return False
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    return len(row) >= 4
        except OSError:
            return False
        return False


def _report_line(c: Candidate) -> str:
    """One report line: verdict, artist — title, and the copy count.

    Loose files are often untagged, so a blank tag falls back to the matched library track's, then
    (title only) to the file name.
    """
    canon = c.group_canon
    artist = c.tags.artist or (canon.artist if canon else None)
    title = c.tags.title or (canon.title if canon else None) or c.best.name
    copies = f"  [dim]\\[{len(c.paths)} copies][/dim]" if len(c.paths) > 1 else ""
    return f"  {c.verdict.value:<7}  {escape(artist or '?')} — {escape(title)}{copies}"


class MusicFolderImporter(Importer):
    name = "folder"
    label = "Music folder"
    provides = DataKind.TRACKS

    def __init__(self, path: Path, *more: Path) -> None:
        """Import `path` and any `more` folders as one batch, so repeats across them collapse."""
        super().__init__(path)
        self.roots = [path, *more]

    @classmethod
    def detect(cls, path: Path) -> bool:
        if not path.is_dir():
            return False
        return any(p.suffix.lower() in AUDIO_EXTENSIONS for p in path.rglob("*") if p.is_file())

    def import_tracks(self, s: Session) -> None:
        """Empty library: scan it all. Otherwise classify, report, import new recordings and upgrades."""
        if not s.scalar(select(exists().where(TrackFile.id.is_not(None)))):
            # Nothing to compare against: the PicardedMusic-style bulk scan, binding left to `land`.
            for root in self.roots:
                MusicScanner(target_path=root).run(s)
            return

        ctx = library_context(s)
        candidates = classify(s, self.roots, ctx=ctx)
        # The report comes before any write, so `-n` (which only rolls back) prints the same thing.
        for c in candidates:
            console.print(_report_line(c))

        to_import = unbound = 0
        for c in candidates:
            if c.verdict is Verdict.HAVE or c.in_library:
                continue
            tf = c.to_trackfile()
            s.add(tf)
            s.flush()
            # Binding now (not at `land`) puts an upgrade in its group, so `organize` can swap it in.
            # None: no rung matched and there is no title to make a Track from (R4). Until it is tagged
            # nothing binds it later either, so it is reported below rather than silently counted.
            unbound += resolve_file(s, tf, ctx) is None
            s.flush()
            to_import += 1

        counts = {v: sum(c.verdict is v for c in candidates) for v in Verdict}
        _done(
            "Files",
            f"[green]{counts[Verdict.NEW]}[/green] new, {counts[Verdict.UPGRADE]} upgrade, "
            # Mode-neutral wording: under -n the same line prints, and the session is rolled back.
            f"[dim]{counts[Verdict.HAVE]} have[/dim] — {to_import} file(s) to import",
        )
        if unbound:
            console.print(
                f"  [yellow]{unbound} new file(s) have no tags:[/yellow] imported unbound — tag them, "
                "then `maint enrich` + `land`"
            )
