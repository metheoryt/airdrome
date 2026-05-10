from pathlib import Path

from alembic.config import Config

from airdrome.conf import settings
from alembic import command


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_LOCATION = _REPO_ROOT / "alembic"


def _build_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", str(settings.db_dsn))
    return cfg


def upgrade_to_head() -> None:
    command.upgrade(_build_config(), "head")
