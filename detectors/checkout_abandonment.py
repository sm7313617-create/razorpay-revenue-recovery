"""
detectors/checkout_abandonment.py
----------------------------------------------------------------
Deterministic detector for abandoned checkout sessions that have not
yet received a recovery action.

No LLM involved — all priority assignment is rule-based.

Functions
---------
detect_abandoned_checkouts(session) -> list[dict]
    Returns abandoned sessions with no existing recovery_action record.

get_abandonment_summary(session)    -> dict
    Returns totals and a per-priority breakdown.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Allow running directly as a script from the project root
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CheckoutSession, RecoveryAction


# ---------------------------------------------------------------------------
# Priority mapping — deterministic, no LLM
# ---------------------------------------------------------------------------

def _derive_priority(cart_value: Decimal) -> str:
    """Return a recovery priority string based on cart value thresholds.

    Rules (deterministic, no LLM):
      - cart_value >= 5000  -> "high"
      - cart_value >= 2000  -> "medium"
      - otherwise           -> "low"

    Args:
        cart_value: The monetary value of the abandoned cart.

    Returns:
        One of ``"high"``, ``"medium"``, or ``"low"``.
    """
    if cart_value >= Decimal("5000"):
        return "high"
    if cart_value >= Decimal("2000"):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_abandoned_checkouts(session: Session) -> list[dict[str, Any]]:
    """Query checkout_sessions for abandoned sessions with no recovery action.

    A session is considered **unprocessed** if its ``id`` does not appear as a
    ``reference_id`` in ``recovery_actions`` for rows where
    ``event_type = 'checkout_abandonment'``.

    ``minutes_since_abandonment`` is computed at call time using
    ``datetime.now(timezone.utc)`` so callers always get a fresh value.

    This function is fully idempotent — running it multiple times returns the
    same result set without mutating any table.

    Args:
        session: An active SQLAlchemy ``Session`` bound to the target database.

    Returns:
        A list of dicts, one per unprocessed abandoned session, each containing:

        - ``session_id``                (uuid.UUID) — primary key of the session
        - ``merchant_id``               (str)       — merchant identifier
        - ``customer_id``               (str)       — customer identifier
        - ``cart_value``                (Decimal)   — cart value at abandonment
        - ``abandoned_at``              (datetime)  — UTC timestamp of abandonment
        - ``minutes_since_abandonment`` (float)     — elapsed minutes since abandonment
        - ``recovery_priority``         (str)       — derived: "high" / "medium" / "low"
    """
    # Sub-query: UUIDs of sessions that already have a recovery action
    already_actioned = select(RecoveryAction.reference_id).where(
        RecoveryAction.event_type == "checkout_abandonment"
    )

    stmt = select(CheckoutSession).where(
        CheckoutSession.status == "abandoned",
        CheckoutSession.id.not_in(already_actioned),
    )

    sessions = session.scalars(stmt).all()

    now_utc = datetime.now(timezone.utc)

    results: list[dict[str, Any]] = []
    for s in sessions:
        # Guard: abandoned_at may theoretically be None even for abandoned rows
        # (e.g., migrated data).  Fall back to created_at as a safe proxy.
        abandoned_at = s.abandoned_at or s.created_at

        # Ensure the timestamp is timezone-aware before arithmetic
        if abandoned_at.tzinfo is None:
            abandoned_at = abandoned_at.replace(tzinfo=timezone.utc)

        elapsed_seconds = (now_utc - abandoned_at).total_seconds()
        minutes_since = round(elapsed_seconds / 60, 2)

        results.append(
            {
                "session_id":                s.id,
                "merchant_id":               s.merchant_id,
                "customer_id":               s.customer_id,
                "cart_value":                s.cart_value,
                "abandoned_at":              abandoned_at,
                "minutes_since_abandonment": minutes_since,
                "recovery_priority":         _derive_priority(s.cart_value),
            }
        )

    return results


def get_abandonment_summary(session: Session) -> dict[str, Any]:
    """Return aggregate statistics for all unprocessed abandoned checkouts.

    Computes total session count, total cart value at risk, and a per-priority
    breakdown (high / medium / low).

    Args:
        session: An active SQLAlchemy ``Session`` bound to the target database.

    Returns:
        A dict with the following keys:

        - ``total_abandoned``  (int)     — count of unprocessed sessions
        - ``total_cart_value`` (Decimal) — sum of cart values for all unprocessed
        - ``by_priority``      (dict)    — per-priority breakdown::

              {
                  "high":   {"count": int, "cart_value": Decimal},
                  "medium": {"count": int, "cart_value": Decimal},
                  "low":    {"count": int, "cart_value": Decimal},
              }
    """
    records = detect_abandoned_checkouts(session)

    by_priority: dict[str, dict[str, Any]] = {
        "high":   {"count": 0, "cart_value": Decimal("0")},
        "medium": {"count": 0, "cart_value": Decimal("0")},
        "low":    {"count": 0, "cart_value": Decimal("0")},
    }
    total_value = Decimal("0")

    for r in records:
        priority = r["recovery_priority"]
        total_value += r["cart_value"]
        by_priority[priority]["count"]      += 1
        by_priority[priority]["cart_value"] += r["cart_value"]

    return {
        "total_abandoned":  len(records),
        "total_cart_value": total_value,
        "by_priority":      by_priority,
    }


# ---------------------------------------------------------------------------
# Script entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    import pandas as pd
    from dotenv import load_dotenv
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # Load environment variables from project-root .env
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(dotenv_path=env_path)

    db_url = os.getenv("DB_URL")
    if not db_url:
        print("[ERROR] DB_URL not set in .env", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with SessionLocal() as session:
        print("\n" + "=" * 60)
        print("  ABANDONED CHECKOUTS — unprocessed")
        print("=" * 60)

        abandoned = detect_abandoned_checkouts(session)

        if abandoned:
            df = pd.DataFrame(abandoned)
            df["session_id"]   = df["session_id"].astype(str)
            df["cart_value"]   = df["cart_value"].apply(lambda x: f"{x:,.2f}")
            df["abandoned_at"] = pd.to_datetime(df["abandoned_at"], utc=True).dt.strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            print(df.to_string(index=False))
        else:
            print("  No unprocessed abandoned checkouts found.")

        print("\n" + "=" * 60)
        print("  ABANDONMENT SUMMARY")
        print("=" * 60)

        summary = get_abandonment_summary(session)
        print(f"\n  Total abandoned (unprocessed) : {summary['total_abandoned']}")
        print(f"  Total cart value at risk (INR): {summary['total_cart_value']:,.2f}")
        print("\n  Breakdown by recovery priority:")

        breakdown_rows = [
            {
                "priority":         priority,
                "count":            data["count"],
                "cart_value (INR)": f"{data['cart_value']:,.2f}",
            }
            for priority, data in summary["by_priority"].items()
        ]

        pprint.pprint(breakdown_rows, indent=4)

    engine.dispose()