"""
detectors/payment_failure.py
----------------------------------------------------------------
Deterministic detector for failed payments that have not yet
received a recovery action.

No LLM involved — all severity assignment is rule-based.

Functions
---------
detect_failed_payments(session)  -> list[dict]
    Returns failed payments with no existing recovery_action record.

get_failure_summary(session)     -> dict
    Returns aggregated counts and amounts grouped by failure_code.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Allow running directly as a script from the project root
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Payment, RecoveryAction


# ---------------------------------------------------------------------------
# Severity mapping — deterministic, no LLM
# ---------------------------------------------------------------------------

_FAILURE_SEVERITY: dict[str, str] = {
    "bank_downtime":      "high",
    "gateway_timeout":    "high",
    "card_declined":      "medium",
    "insufficient_funds": "low",
}


def _derive_severity(failure_code: str | None) -> str:
    """Return a severity string for *failure_code* using a fixed rule table.

    Args:
        failure_code: The failure code string from the payments table,
                      or ``None`` if not set.

    Returns:
        One of ``"high"``, ``"medium"``, or ``"low"``.
        Defaults to ``"low"`` for unknown / null codes.
    """
    if failure_code is None:
        return "low"
    return _FAILURE_SEVERITY.get(failure_code, "low")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_failed_payments(session: Session) -> list[dict[str, Any]]:
    """Query the payments table for failed payments with no recovery action.

    A payment is considered **unprocessed** if its ``id`` does not appear as a
    ``reference_id`` in ``recovery_actions`` for rows where
    ``event_type = 'payment_failure'``.

    This function is fully idempotent — running it multiple times returns the
    same result set without mutating any table.

    Args:
        session: An active SQLAlchemy ``Session`` bound to the target database.

    Returns:
        A list of dicts, one per unprocessed failed payment, each containing:

        - ``payment_id``   (uuid.UUID)   — primary key of the payment
        - ``merchant_id``  (str)         — merchant identifier
        - ``customer_id``  (str)         — customer identifier
        - ``amount``       (Decimal)     — payment amount
        - ``failure_code`` (str | None)  — reason the payment failed
        - ``created_at``   (datetime)    — UTC timestamp of the payment attempt
        - ``severity``     (str)         — derived: "high" / "medium" / "low"
    """
    # Sub-query: UUIDs of payments that already have a recovery action
    already_actioned = select(RecoveryAction.reference_id).where(
        RecoveryAction.event_type == "payment_failure"
    )

    stmt = select(Payment).where(
        Payment.status == "failed",
        Payment.id.not_in(already_actioned),
    )

    payments = session.scalars(stmt).all()

    results: list[dict[str, Any]] = []
    for p in payments:
        results.append(
            {
                "payment_id":   p.id,
                "merchant_id":  p.merchant_id,
                "customer_id":  p.customer_id,
                "amount":       p.amount,
                "failure_code": p.failure_code,
                "created_at":   p.created_at,
                "severity":     _derive_severity(p.failure_code),
            }
        )

    return results


def get_failure_summary(session: Session) -> dict[str, Any]:
    """Return aggregate statistics for all unprocessed failed payments.

    Computes the count of unprocessed failed payments grouped by
    ``failure_code``, plus totals across all failure codes.

    Args:
        session: An active SQLAlchemy ``Session`` bound to the target database.

    Returns:
        A dict with the following keys:

        - ``total_failed``         (int)     — total unprocessed failed payments
        - ``total_amount_at_risk`` (Decimal) — sum of amounts for all unprocessed
        - ``by_failure_code``      (dict)    — per-code breakdown::

              {
                  "<failure_code>": {
                      "count":  int,
                      "amount": Decimal,
                  },
                  ...
              }
    """
    records = detect_failed_payments(session)

    by_code: dict[str, dict[str, Any]] = {}
    total_amount = Decimal("0")

    for r in records:
        code = r["failure_code"] or "unknown"
        total_amount += r["amount"]

        if code not in by_code:
            by_code[code] = {"count": 0, "amount": Decimal("0")}

        by_code[code]["count"]  += 1
        by_code[code]["amount"] += r["amount"]

    return {
        "total_failed":         len(records),
        "total_amount_at_risk": total_amount,
        "by_failure_code":      by_code,
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
        print("  FAILED PAYMENTS — unprocessed")
        print("=" * 60)

        failures = detect_failed_payments(session)

        if failures:
            df = pd.DataFrame(failures)
            df["payment_id"] = df["payment_id"].astype(str)
            df["amount"] = df["amount"].apply(lambda x: f"{x:,.2f}")
            df["created_at"] = pd.to_datetime(df["created_at"], utc=True).dt.strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            print(df.to_string(index=False))
        else:
            print("  No unprocessed failed payments found.")

        print("\n" + "=" * 60)
        print("  FAILURE SUMMARY")
        print("=" * 60)

        summary = get_failure_summary(session)
        print(f"\n  Total failed (unprocessed) : {summary['total_failed']}")
        print(f"  Total amount at risk (INR) : {summary['total_amount_at_risk']:,.2f}")
        print("\n  Breakdown by failure code:")

        breakdown_rows = [
            {
                "failure_code": code,
                "count":        data["count"],
                "amount (INR)": f"{data['amount']:,.2f}",
            }
            for code, data in summary["by_failure_code"].items()
        ]

        if breakdown_rows:
            pprint.pprint(breakdown_rows, indent=4)
        else:
            print("  (none)")

    engine.dispose()