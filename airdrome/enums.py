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
