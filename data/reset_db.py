"""
data/reset_db.py
----------------------------------------------------------------
Hard-reset the database to the canonical seed state (Task 2 baseline).

WARNING: This is a DESTRUCTIVE, IRREVERSIBLE operation.
   - Truncates ALL four tables: payments, checkout_sessions,
     recovery_actions, audit_log.
   - Re-seeds payments (60 rows) and checkout_sessions (40 rows)
     from data/seed_data.py using random.seed(42) / Faker.seed(42)
     for full reproducibility.
   - recovery_actions and audit_log are left empty (no pipeline
     runs have occurred yet).

This script should only be run deliberately before a clean demo or
metrics baseline collection.  It must NOT be run from any test suite
or automated pipeline step.

Usage
-----
    python -m data.reset_db          # from project root
    python data/reset_db.py          # direct execution
"""

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Allow running as a script from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.seed_data import reset_seed, seed_checkout_sessions, seed_payments  # noqa: E402


def reset_and_reseed(db_url: str) -> None:
    """Truncate all four tables and re-run the canonical seed."""
    engine = create_engine(db_url, echo=False, future=True)

    with engine.connect() as conn:
        print("\n--- DB Hard Reset -------------------------------------------")
        print(f"  Target : {db_url.split('@')[-1]}")
        print()

        # Verify connectivity
        conn.execute(text("SELECT 1"))

        # Truncate in dependency-safe order (no FK cascade needed since
        # recovery_actions.reference_id has no DB-level FK constraint).
        for table in ("audit_log", "recovery_actions", "payments", "checkout_sessions"):
            before = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            conn.execute(text(f"TRUNCATE TABLE {table}"))
            conn.commit()
            print(f"  [TRUNCATED] {table:<25s} was {before} rows -> 0 rows")

    engine.dispose()
    print()

    # Re-seed using the canonical seed functions (same random.seed(42))
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(db_url, echo=False, future=True)
    SessionFactory = sessionmaker(bind=engine)

    with SessionFactory() as session:
        print("  Re-seeding payments and checkout_sessions ...")
        reset_seed(42)

        payments = seed_payments(session)
        session.flush()
        failed = [p for p in payments if p.status == "failed"]
        success = [p for p in payments if p.status == "success"]
        print(f"    [OK] {len(payments):3d} payments inserted  ({len(success)} success / {len(failed)} failed)")

        sessions = seed_checkout_sessions(session)
        session.flush()
        abandoned = [s for s in sessions if s.status == "abandoned"]
        completed = [s for s in sessions if s.status == "completed"]
        print(f"    [OK] {len(sessions):3d} checkout sessions  ({len(completed)} completed / {len(abandoned)} abandoned)")

        session.commit()
        print("\n  All seed rows committed.")

    engine.dispose()

    # Final count verification
    engine = create_engine(db_url, echo=False, future=True)
    with engine.connect() as conn:
        print()
        print("  Final row counts:")
        for table in ("payments", "checkout_sessions", "recovery_actions", "audit_log"):
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            print(f"    {table:<25s} : {cnt}")
    engine.dispose()

    print()
    print("  DB reset complete.  Clean seed state restored.")
    print("  recovery_actions = 0  (no pipeline runs)")
    print("  audit_log        = 0  (no pipeline runs)")
    print()


if __name__ == "__main__":
    load_dotenv()
    db_url = os.getenv("DB_URL")
    if not db_url:
        print("[ERROR] DB_URL is not set.", file=sys.stderr)
        sys.exit(1)

    # Safety prompt
    print("\n!!! WARNING: This will TRUNCATE payments, checkout_sessions,")
    print("             recovery_actions, and audit_log, then re-seed. !!!")
    confirm = input("\nType YES to proceed: ").strip()
    if confirm != "YES":
        print("Aborted.")
        sys.exit(0)

    try:
        reset_and_reseed(db_url)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[ERROR] Reset failed: {exc}", file=sys.stderr)
        sys.exit(1)
