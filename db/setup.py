"""
db/setup.py
----------------------------------------------------------------
Creates all database tables defined in db/models.py.

Usage
-----
    python -m db.setup          # from project root
    python db/setup.py          # direct execution
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

# Allow running as a script from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import Base  # noqa: E402  (import after path fix)


def create_tables(db_url: str) -> None:
    """Create all tables declared in Base.metadata if they do not already exist."""
    engine = create_engine(db_url, echo=False, future=True)

    # Verify connectivity before attempting DDL
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    # Determine which tables exist before creation so we can report new ones
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    Base.metadata.create_all(engine)

    # Report results
    inspector = inspect(engine)
    all_tables = set(inspector.get_table_names())
    new_tables = all_tables - existing_tables

    print("\n--- Database Setup -------------------------------------------")
    print(f"  Database : {db_url.split('@')[-1]}")   # hide credentials
    print(f"  Tables already present : {len(existing_tables)}")
    print(f"  Tables created this run: {len(new_tables)}")
    print()

    for table in sorted(all_tables):
        marker = "[NEW]   " if table in new_tables else "[exists]"
        print(f"  {marker}  {table}")

    print("\n  All tables are ready.\n")
    engine.dispose()


if __name__ == "__main__":
    # Load .env from the project root (two levels up from this file)
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(dotenv_path=env_path)

    db_url = os.getenv("DB_URL")
    if not db_url:
        print(
            "[ERROR] DB_URL is not set. "
            "Add it to your .env file and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        create_tables(db_url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"\n[ERROR] Failed to connect to the database or create tables.\n"
            f"  Reason : {exc}\n\n"
            f"  Check that:\n"
            f"    * PostgreSQL is running\n"
            f"    * DB_URL in .env is correct\n"
            f"    * The database '{db_url.rsplit('/', 1)[-1]}' exists\n",
            file=sys.stderr,
        )
        sys.exit(1)
