from pathlib import Path

from pydantic import PostgresDsn, DirectoryPath, FilePath
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_dsn: PostgresDsn = "postgresql+psycopg://postgres:postgres@localhost:5437/postgres"
    db_echo: bool = False
    duplicates_filepath: FilePath = Path("data") / "duplicates.json"
    apple_music_library_dirpath: DirectoryPath = r"C:\Users\methe\Music\iTunes\iTunes Media\Music"
    apple_music_library_xml_filepath: FilePath = Path("data") / "apple" / "AppleMusicLibrary.xml"
    apple_music_play_activity_filepath: FilePath = Path("data") / "apple" / "Apple Music Play Activity.csv"
    lastfm_scrobbles_filepath: FilePath = Path("data") / "lastfm" / "MeTheoryT.csv"
    spotify_streaming_history_dirpath: FilePath = Path("data") / "spotify"


settings = Settings()
