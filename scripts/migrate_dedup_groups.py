"""One-time migration of the stored dedup groups to a v2 pair-decision document.

Usage: `uv run python scripts/migrate_dedup_groups.py OUT [--dsn DSN]`.

Read-only against the DB: the first statement is `SET TRANSACTION READ ONLY`, nothing is committed and the
transaction is rolled back. It never applies migrations. `OUT` must not exist and must not be the live
mirror path; the document is moved into place by hand once the printed report has been reviewed.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from airdrome.conf import settings
from airdrome.normalize.dedup.legacy import check_out, format_report, write_migration


def main(argv: list[str] | None = None) -> int:
    """Run the migration read-only, write OUT, print the report; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="v2 document to write (must not exist)")
    parser.add_argument("--dsn", default=None, help="SQLAlchemy DSN (default: DB_DSN from settings)")
    args = parser.parse_args(argv)
    try:
        check_out(args.out)  # before connecting
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    engine = create_engine(args.dsn or str(settings.db_dsn))
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))  # first statement of the only transaction
            try:
                m = write_migration(conn, args.out, datetime.now(UTC))
            finally:
                conn.rollback()
    finally:
        engine.dispose()
    print(format_report(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
