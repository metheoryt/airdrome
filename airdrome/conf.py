from pathlib import Path

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_dsn: PostgresDsn
    db_echo: bool = False

    library_dir: Path = Field(
        description="Airdrome-organized library path. Must be empty for a fresh install."
    )

    # Manual dedup choices live in the DB (DedupGroup), which is disposable; this
    # overrides where their durable mirror is kept. Unset means under the library —
    # see `duplicates_file`.
    duplicates_filepath: Path | None = None

    # navidrome
    navidrome_db_dsn: str | None = None
    navidrome_user: str | None = None
    navidrome_port: int = 4533

    @property
    def duplicates_file(self) -> Path:
        """Where the dedup mirror lives — under the library unless overridden.

        Colocating it with the library it describes makes it per-library by
        construction (two libraries cannot clobber each other's canons), and makes
        it travel and back up with that library instead of with the checkout.
        """
        return self.duplicates_filepath or self.library_dir / ".airdrome" / "duplicates.json"


settings = Settings()
