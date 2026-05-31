from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import airdrome.cloud.sources
import airdrome.models  # noqa: F401
from airdrome.conf import settings
from airdrome.models import AirdromeBase, PathType
from alembic import context


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(settings.db_dsn))

target_metadata = AirdromeBase.metadata


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, PathType):
        autogen_context.imports.add("import airdrome.models")
        return "airdrome.models.PathType()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
