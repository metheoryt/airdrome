import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

from airdrome.conf import settings


def _test_db_url() -> str:
    """Derive test DB URL: same as DB_DSN but with '_test' appended to the database name."""
    url = str(settings.db_dsn)
    base, db_name = url.rsplit("/", 1)
    return f"{base}/{db_name}_test"


@pytest.fixture(scope="session")
def test_engine():
    test_url = _test_db_url()
    test_db_name = test_url.rsplit("/", 1)[1]

    # Connect to the main DB to create the test DB if it doesn't exist
    admin_engine = create_engine(str(settings.db_dsn), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": test_db_name}
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{test_db_name}"'))
    admin_engine.dispose()

    engine = create_engine(test_url, echo=settings.db_echo)

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()

    # Ensure all model metadata is registered before creating tables
    import airdrome.cloud.apple.models  # noqa: F401
    import airdrome.models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    yield engine

    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def session(test_engine):
    """
    Provide a session that rolls back all changes after each test.

    Uses a nested transaction (SAVEPOINT) so the outer transaction can be rolled back,
    leaving the database pristine for the next test.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    nested = connection.begin_nested()

    yield session

    session.close()
    nested.rollback()
    transaction.rollback()
    connection.close()
