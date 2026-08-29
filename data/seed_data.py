"""
data/seed_data.py
----------------------------------------------------------------
Generates and inserts realistic synthetic data for the Razorpay
AI Revenue Recovery demo dataset.

Seeded data
-----------
Payments         : 60 records  (25 success, 35 failed)
  failure dist   : 12 insufficient_funds, 10 card_declined,
                    8 gateway_timeout, 5 bank_downtime
Checkout sessions: 40 records  (18 completed, 22 abandoned)
  abandoned_at   : created_at + 15-90 minutes
  cart_value     : Rs.500 - Rs.15,000

Usage
-----
    python -m data.seed_data    # from project root
    python data/seed_data.py    # direct execution
"""

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from faker import Faker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Allow running as a script from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import CheckoutSession, Payment  # noqa: E402

# ----------------------------------------------------------------
# Reproducibility
# ----------------------------------------------------------------
def reset_seed(seed: int = 42) -> None:
    """Reset RNG seeds for exact reproducibility across runs."""
    random.seed(seed)
    fake.seed_instance(seed)


fake = Faker("en_IN")
reset_seed(42)

# ----------------------------------------------------------------
# Constants
# ----------------------------------------------------------------

FAILURE_DISTRIBUTION: list[tuple[str, int]] = [
    ("insufficient_funds", 12),
    ("card_declined", 10),
    ("gateway_timeout", 8),
    ("bank_downtime", 5),
]

TOTAL_FAILED = sum(count for _, count in FAILURE_DISTRIBUTION)   # 35
TOTAL_SUCCESS = 25
TOTAL_PAYMENTS = TOTAL_SUCCESS + TOTAL_FAILED                     # 60

TOTAL_COMPLETED = 18
TOTAL_ABANDONED = 22
TOTAL_SESSIONS = TOTAL_COMPLETED + TOTAL_ABANDONED                # 40

CART_MIN = 500.00
CART_MAX = 15_000.00
PAYMENT_AMOUNT_MIN = 100.00
PAYMENT_AMOUNT_MAX = 50_000.00

# Seed data spread over the last 30 days
NOW = datetime.now(timezone.utc)
WINDOW_DAYS = 30


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _seeded_uuid() -> uuid.UUID:
    """Generate a deterministic RFC 4122 v4 UUID using Python's seeded random generator."""
    return uuid.UUID(int=random.getrandbits(128), version=4)


def _random_past_dt(days: int = WINDOW_DAYS) -> datetime:
    """Return a random timezone-aware datetime within the last *days* days."""
    delta_seconds = random.randint(0, days * 24 * 3600)
    return NOW - timedelta(seconds=delta_seconds)


def _merchant_id() -> str:
    """Generate a realistic Razorpay-style merchant ID."""
    return f"MID_{fake.bothify('??######').upper()}"


def _customer_id() -> str:
    """Generate a realistic customer ID."""
    return f"CUS_{fake.bothify('??######').upper()}"


def _amount(lo: float = PAYMENT_AMOUNT_MIN, hi: float = PAYMENT_AMOUNT_MAX) -> float:
    """Return a random amount rounded to 2 decimal places."""
    return round(random.uniform(lo, hi), 2)


# ----------------------------------------------------------------
# Seeding functions
# ----------------------------------------------------------------

def seed_payments(session) -> list[Payment]:
    """
    Insert 60 Payment records:
      - 25 successful
      - 35 failed, distributed as per FAILURE_DISTRIBUTION
    Returns the list of inserted Payment objects.
    """
    payments: list[Payment] = []

    # Build the failure_code list in the exact required distribution
    failure_codes: list[str] = []
    for code, count in FAILURE_DISTRIBUTION:
        failure_codes.extend([code] * count)
    random.shuffle(failure_codes)   # shuffle so insertion order is unpredictable

    # 35 failed payments
    for fc in failure_codes:
        created = _random_past_dt()
        p = Payment(
            id=_seeded_uuid(),
            merchant_id=_merchant_id(),
            customer_id=_customer_id(),
            amount=_amount(),
            currency="INR",
            status="failed",
            failure_code=fc,
            created_at=created,
            updated_at=created,
        )
        payments.append(p)

    # 25 successful payments
    for _ in range(TOTAL_SUCCESS):
        created = _random_past_dt()
        p = Payment(
            id=_seeded_uuid(),
            merchant_id=_merchant_id(),
            customer_id=_customer_id(),
            amount=_amount(),
            currency="INR",
            status="success",
            failure_code=None,
            created_at=created,
            updated_at=created,
        )
        payments.append(p)

    random.shuffle(payments)  # mix success/failed rows before bulk insert
    session.bulk_save_objects(payments)
    return payments


def seed_checkout_sessions(session) -> list[CheckoutSession]:
    """
    Insert 40 CheckoutSession records:
      - 18 completed
      - 22 abandoned (abandoned_at = created_at + 15-90 minutes)
    Returns the list of inserted CheckoutSession objects.
    """
    sessions: list[CheckoutSession] = []

    # 22 abandoned sessions
    for _ in range(TOTAL_ABANDONED):
        created = _random_past_dt()
        abandon_offset = timedelta(minutes=random.randint(15, 90))
        cs = CheckoutSession(
            id=_seeded_uuid(),
            merchant_id=_merchant_id(),
            customer_id=_customer_id(),
            cart_value=round(random.uniform(CART_MIN, CART_MAX), 2),
            status="abandoned",
            abandoned_at=created + abandon_offset,
            created_at=created,
        )
        sessions.append(cs)

    # 18 completed sessions
    for _ in range(TOTAL_COMPLETED):
        created = _random_past_dt()
        cs = CheckoutSession(
            id=_seeded_uuid(),
            merchant_id=_merchant_id(),
            customer_id=_customer_id(),
            cart_value=round(random.uniform(CART_MIN, CART_MAX), 2),
            status="completed",
            abandoned_at=None,
            created_at=created,
        )
        sessions.append(cs)

    random.shuffle(sessions)
    session.bulk_save_objects(sessions)
    return sessions


# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------

def run_seed(db_url: str) -> None:
    """Connect, create a session, insert all seed rows, and commit."""
    engine = create_engine(db_url, echo=False, future=True)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        print("\n--- Seeding Database -----------------------------------------")
        print(f"  Target : {db_url.split('@')[-1]}")

        # -- Payments --------------------------------------------------
        print(f"\n  Inserting {TOTAL_PAYMENTS} payments ...", end="", flush=True)
        payments = seed_payments(session)
        session.flush()   # materialise rows before counting

        failed_payments = [p for p in payments if p.status == "failed"]
        success_payments = [p for p in payments if p.status == "success"]
        print(" done.")
        print(f"    [OK] {len(success_payments):3d}  successful")
        print(f"    [OK] {len(failed_payments):3d}  failed")

        # Verify failure-code distribution
        fc_counts: dict[str, int] = {}
        for p in failed_payments:
            fc_counts[p.failure_code] = fc_counts.get(p.failure_code, 0) + 1
        for code, expected in FAILURE_DISTRIBUTION:
            actual = fc_counts.get(code, 0)
            print(f"        - {code:<22s} {actual} (expected {expected})")

        # -- Checkout Sessions -----------------------------------------
        print(f"\n  Inserting {TOTAL_SESSIONS} checkout sessions ...", end="", flush=True)
        sessions_data = seed_checkout_sessions(session)
        session.flush()

        abandoned = [s for s in sessions_data if s.status == "abandoned"]
        completed = [s for s in sessions_data if s.status == "completed"]
        print(" done.")
        print(f"    [OK] {len(completed):3d}  completed")
        print(f"    [OK] {len(abandoned):3d}  abandoned")

        # -- Commit atomically -----------------------------------------
        session.commit()
        print("\n  All records committed successfully.")
        print("\n  Summary")
        print(f"    Payments          : {len(payments)}")
        print(f"    Checkout sessions : {len(sessions_data)}")
        print(f"    Total rows        : {len(payments) + len(sessions_data)}\n")

    engine.dispose()


if __name__ == "__main__":
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
        run_seed(db_url)
    except Exception as exc:  # noqa: BLE001
        print(
            f"\n[ERROR] Seeding failed.\n"
            f"  Reason : {exc}\n\n"
            f"  Make sure you have run db/setup.py first to create the tables.\n",
            file=sys.stderr,
        )
        sys.exit(1)
