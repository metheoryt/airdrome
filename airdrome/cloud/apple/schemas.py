from pydantic import BaseModel, ConfigDict, Field

from .models import ApplePlaylistBase


class ApplePlaylistImport(ApplePlaylistBase):
    model_config = ConfigDict(extra="forbid")

    # don't put those into the database
    smart_info: bytes | None = Field(None, alias="Smart Info")
    smart_criteria: bytes | None = Field(None, alias="Smart Criteria")
    items: list[PlaylistTrackSchema] = Field(alias="Playlist Items")


class PlaylistTrackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apple_track_id: int = Field(alias="Track ID")
