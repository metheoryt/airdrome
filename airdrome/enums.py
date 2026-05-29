from enum import StrEnum


class Platform(StrEnum):
    SPOTIFY = "spotify"
    LASTFM = "lastfm"
    APPLE = "apple"
    LISTENBRAINZ = "listenbrainz"
    NAVIDROME = "navidrome"


class Provider(StrEnum):
    """The concrete export a source track/playlist was ingested from."""

    APPLE_XML = "apple_xml"
    APPLE_MS = "apple_ms"
