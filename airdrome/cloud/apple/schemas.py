from pydantic import BaseModel, ConfigDict, Field


class ApplePlaylistBase(BaseModel):
    """Pydantic model for Apple playlist import data (iTunes XML)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias="Name")
    playlist_id: int = Field(alias="Playlist ID")
    persistent_id: str = Field(alias="Playlist Persistent ID")
    description: str = Field(alias="Description")
    all_items: bool = Field(alias="All Items")
    parent_persistent_id: str | None = Field(None, alias="Parent Persistent ID")

    master: bool = Field(False, alias="Master")
    visible: bool = Field(True, alias="Visible")
    music: bool = Field(False, alias="Music")
    folder: bool = Field(False, alias="Folder")
    distinguished_kind: int | None = Field(None, alias="Distinguished Kind")
    favorited: bool = Field(False, alias="Favorited")
    loved: bool = Field(False, alias="Loved")


class PlaylistTrackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apple_track_id: int = Field(alias="Track ID")


class ApplePlaylistImport(ApplePlaylistBase):
    model_config = ConfigDict(extra="forbid")

    # don't put those into the database
    smart_info: bytes | None = Field(None, alias="Smart Info")
    smart_criteria: bytes | None = Field(None, alias="Smart Criteria")
    items: list[PlaylistTrackSchema] = Field(alias="Playlist Items")
