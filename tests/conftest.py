import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from airdrome.conf import settings
from airdrome.models import AirdromeBase


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
    import airdrome.cloud.sources
    import airdrome.models  # noqa: F401

    AirdromeBase.metadata.create_all(engine)

    yield engine

    AirdromeBase.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def session(test_engine):
    """Provide a session that rolls back all changes after each test.

    The session joins an outer transaction via a SAVEPOINT (`create_savepoint`), so even
    code under test that calls `session.commit()` only releases/restarts the savepoint —
    the outer transaction is rolled back at teardown, leaving the database pristine.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    session.close()
    transaction.rollback()
    connection.close()


def write_wav(path, seed: int, seconds: int = 30, gain: float = 1.0, rate: int = 44100):
    """Write a deterministic seeded tone/chord sequence as a mono 16-bit WAV at `path`; return it.

    A plain function (the `make_wav` fixture returns it) so module-scoped fixtures can build their
    wavs once — pure-Python synthesis is slow.
    """
    import math
    import random
    import struct
    import wave

    # Band-limited noise fingerprints near-identically for any seed (flat spectrum), so the
    # signal is a seeded chord sequence instead: distinct per seed, independent of `rate`.
    rng = random.Random(seed)
    seg = 0.2
    samples: list[float] = []
    for _ in range(int(seconds / seg)):
        freqs = [110 * 2 ** (rng.randrange(0, 48) / 12) for _ in range(3)]
        n = int(seg * rate)
        for i in range(n):
            t = i / rate
            env = min(1.0, i / (0.01 * rate), (n - i) / (0.01 * rate))
            samples.append(env * sum(math.sin(2 * math.pi * f * t) for f in freqs) / 3)
    frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v * gain)) * 32767)) for v in samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


@pytest.fixture()
def make_wav():
    """Factory writing a deterministic seeded tone/chord sequence as a mono 16-bit WAV."""
    return write_wav
