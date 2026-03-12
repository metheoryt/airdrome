from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class LastFMScrobble(BaseModel):
    artist: str | None = Field(None)
    album: str | None = Field(None)
    title: str
    date: datetime

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, value):
        if isinstance(value, str):
            return datetime.strptime(value, "%d %b %Y %H:%M")  # Custom format
        return value

    @property
    def full_name(self):
        return f"{self.artist or ''} [{self.album or ''}] {self.title}"
