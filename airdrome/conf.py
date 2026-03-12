from pathlib import Path

from pydantic import DirectoryPath, FilePath, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_dsn: PostgresDsn = "postgresql+psycopg://postgres:postgres@localhost:5437/postgres"
    db_echo: bool = False

    # local directories
    # apple_music_library_dirpath: DirectoryPath = r"C:\Users\methe\Music\iTunes\iTunes Media\Music"
    apple_music_library_dirpath: DirectoryPath = r"C:\Users\methe\Music\Airdrome\Music"  # testing on a copy
    local_library_copies_dirpath: DirectoryPath = r"C:\Users\methe\Music\Airdrome\Copies"
    local_library_dirpath: DirectoryPath = r"C:\Users\methe\Music\Airdrome\NewLibrary"

    # data to ingest
    apple_music_library_xml_filepath: FilePath = Path("data") / "apple" / "AppleMusicLibrary.xml"
    apple_music_play_activity_filepath: FilePath = Path("data") / "apple" / "Apple Music Play Activity.csv"
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
