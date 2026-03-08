from pydantic import BaseModel, ConfigDict, Field


class PlaylistTrackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apple_track_id: int = Field(alias="Track ID")
