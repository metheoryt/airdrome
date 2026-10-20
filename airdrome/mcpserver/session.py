"""One DB session per tool call — the only place in the MCP server that commits."""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from airdrome.conf import settings
from airdrome.normalize.dedup.mirror import install_mirror


@contextmanager
def tool_session(engine: Engine) -> Generator[Session]:
    """A session that commits on normal exit, rolls back and re-raises on error, and always closes."""
    # expire_on_commit=False, as in the CLI: tools build their answer from ORM objects after the
    # domain call, and a commit must not turn them into lazy reloads.
    session = Session(engine, expire_on_commit=False)
    # Read at call time, not when the server is built, so an override of the path applies at once.
    install_mirror(session, settings.duplicates_file)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
