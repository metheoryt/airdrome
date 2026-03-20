from pathlib import Path

from pydantic import DirectoryPath, Field, FilePath, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_dsn: PostgresDsn = "postgresql+psycopg://postgres:postgres@localhost:5437/postgres"
    db_echo: bool = False

    library_dir: Path = Field(
        r"C:\Users\methe\Music\Airdrome",
        description="Airdrome-organized library path. Must be empty for a fresh install.",
    )

    # data ingest
    lastfm_scrobbles_filepath: FilePath = Path("data") / "lastfm" / "MeTheoryT.csv"
    spotify_streaming_history_dirpath: DirectoryPath = Path("data") / "spotify"
    listenbrainz_listens_dir_path: Path = (
        Path("data") / "listenbrainz" / "listenbrainz_metheoryt_1772562030" / "listens"
    )

    # app-produced data
    duplicates_filepath: Path = Path("data") / "duplicates.json"

    # navidrome
    navidrome_db_dsn: str = "sqlite:///C:/Navidrome/navidrome.db"


settings = Settings()
