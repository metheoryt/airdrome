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

    # Legacy: manual dedup choices now live in the DB (DedupGroup). Retained
    # only so the one-time Alembic import migration can locate the old file.
    duplicates_filepath: Path = Path("data") / "duplicates.json"

    # navidrome
    navidrome_db_dsn: str | None = None
    navidrome_user: str | None = None
    navidrome_port: int = 4533


settings = Settings()
