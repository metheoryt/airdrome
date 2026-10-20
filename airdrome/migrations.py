from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine

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


SchemaState = Literal["head", "behind", "unknown"]


def schema_state() -> SchemaState:
    """Where the DB's alembic revision stands against the script directory, without migrating.

    `behind` can be fixed by `alembic upgrade head` (including a DB with no revision at all);
    `unknown` is a revision the scripts don't know — e.g. one stamped before the migrations were
    squashed — which no upgrade can reach, so the DB has to be recreated.
    """
    engine = create_engine(str(settings.db_dsn))
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    script = ScriptDirectory.from_config(_build_config())
    if current == script.get_current_head():
        return "head"
    if current is None:
        return "behind"
    try:
        known = script.get_revision(current) is not None
    except CommandError:  # alembic raises (ResolutionError) rather than returning None
        known = False
    return "behind" if known else "unknown"


def is_at_head() -> bool:
    """True when the live DB's alembic revision equals the script directory's head."""
    return schema_state() == "head"
