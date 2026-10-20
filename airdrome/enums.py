from enum import StrEnum


class Source(StrEnum):
    """A concrete ingestion source: the export (or service) a record was read from.

    Apple ships two distinct exports — the iTunes Library XML and the Media Services
    package — and the same track legitimately appears in both, so Apple has two values.
    `.service` collapses them back to the coarse service identity ("apple") for the few
    places that care about the platform rather than the export.
    """

    APPLE_XML = "apple_xml"
    APPLE_MS = "apple_ms"
    SPOTIFY = "spotify"
    LASTFM = "lastfm"
    LISTENBRAINZ = "listenbrainz"
    NAVIDROME = "navidrome"

    @property
    def service(self) -> str:
        return _SERVICE[self]


_SERVICE: dict[Source, str] = {
    Source.APPLE_XML: "apple",
    Source.APPLE_MS: "apple",
    Source.SPOTIFY: "spotify",
    Source.LASTFM: "lastfm",
    Source.LISTENBRAINZ: "listenbrainz",
    Source.NAVIDROME: "navidrome",
}


class BindMethod(StrEnum):
    """How an identity link (file->track, alias->track, twin->canon) was established.

    The rungs of the identity ladder, strongest evidence first, plus the manual override.
    On `TrackFile.mbid_source` only MBID (read from a tag) and ACOUSTID (looked up) occur.
    """

    MBID = "mbid"
    ACOUSTID = "acoustid"
    FINGERPRINT = "fingerprint"
    TAGS_EXACT = "tags_exact"
    TAGS_FUZZY = "tags_fuzzy"
    APPLE_PATH = "apple_path"
    DEDUP_FLAGS = "dedup_flags"
    MANUAL = "manual"


class Verdict(StrEnum):
    """The owner's call on a track or alias that is not in the library: want it, or never ask again."""

    WANTED = "wanted"
    SKIP = "skip"


class DecisionKind(StrEnum):
    """What a manual pair decision says about two tracks: one recording, or two."""

    SAME = "same"
    DIFFERENT = "different"


class DecisionOrigin(StrEnum):
    """Where a decision came from (informational): the MCP server, a file import, or the migration."""

    MCP = "mcp"
    IMPORT = "import"
    MIGRATED = "migrated"
