from datetime import datetime

from pydantic import BaseModel, model_validator, Field

from jellyfist.normalize.norm import normalize_name


class TrackAliasSchema(BaseModel):
    artist: str | None = Field(None)
    album: str | None = Field(None)
    title: str | None = Field(None)

    # normalized
    artist_norm: str = Field("")
    album_norm: str = Field("")
    title_norm: str = Field("")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        for f, nf in ("artist", "artist_norm"), ("album", "album_norm"), ("title", "title_norm"):
            if f in data:
                if not data[f]:
                    data[f] = None
                data[nf] = normalize_name(data[f])
        return data

    @property
    def repr(self):
        return f"[{self.artist or ''}/{self.album or ''}/{self.title}]"

    @property
    def id(self):
        return f"{self.artist_norm}-{self.album_norm}-{self.title_norm}"


class TrackScrobble(BaseModel):
    alias: TrackAliasSchema
    date: datetime
