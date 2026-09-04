"""
run_pipeline.py
----------------------------------------------------------------
Production pipeline entry point for Salvage.

Runs the full recovery pipeline in a single pass:
  1. Detect all unprocessed payment failures
     (payments.status = 'failed' with no existing recovery_action)
  2. Detect all unprocessed abandoned checkout sessions
     (checkout_sessions.status = 'abandoned' with no existing recovery_action)
  3. Run the LangGraph agent (run_batch) on all detected events
  4. Print an outcome summary

This is the canonical command to invoke for:
  - Clean demo runs (Task 10)
  - Generating metrics reference numbers (Task 6 / 9)
  - Smoke-testing after a DB reset

!! DB-MUTATING SCRIPT — READ BEFORE RUNNING !!
-----------------------------------------------
Every run inserts rows into:
  - recovery_actions  (one row per processed event)
  - audit_log         (one row per processed event)
  - payments          (may UPDATE status='success' when retry succeeds)

To restore a clean seed state before re-running:
    python -m data.reset_db

Usage
-----
    python run_pipeline.py              # from project root
    python -m run_pipeline              # equivalent

Limiting scope (for testing a subset):
    Edit MAX_PAYMENT_EVENTS / MAX_CHECKOUT_EVENTS below (None = all).
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

load_dotenv()

from agent.graph import run_batch
from detectors.checkout_abandonment import detect_abandoned_checkouts
from detectors.payment_failure import detect_failed_payments

# ---------------------------------------------------------------------------
# Config — set to an integer to cap events processed per type, None = all
# ---------------------------------------------------------------------------
MAX_PAYMENT_EVENTS: int | None = None
MAX_CHECKOUT_EVENTS: int | None = None


def main() -> None:
    """Run the full recovery pipeline on all unprocessed events."""
    db_url = os.environ.get("DB_URL")
    if not db_url:
        print("[ERROR] DB_URL is not set in environment.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url, echo=False, future=True)

    sep = "=" * 65

    print(f"\n{sep}")
    print("  SALVAGE -- Full Pipeline Run")
    print(f"  Started : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(sep)

    # ------------------------------------------------------------------
    # Step 1 — Detect events
    # ------------------------------------------------------------------
    with Session(engine) as session:
        payment_events = detect_failed_payments(session)
        checkout_events = detect_abandoned_checkouts(session)

    if MAX_PAYMENT_EVENTS is not None:
        payment_events = payment_events[:MAX_PAYMENT_EVENTS]
    if MAX_CHECKOUT_EVENTS is not None:
        checkout_events = checkout_events[:MAX_CHECKOUT_EVENTS]

    print(f"\n  Detected {len(payment_events):3d} unprocessed payment failure(s)")
    print(f"  Detected {len(checkout_events):3d} unprocessed abandoned checkout(s)")
    total_events = len(payment_events) + len(checkout_events)
    print(f"  Total   {total_events:3d} event(s) to process\n")

    if total_events == 0:
        print("  Nothing to do — all events have already been processed.")
        print("  To reset: python -m data.reset_db\n")
        engine.dispose()
        return

    # ------------------------------------------------------------------
    # Step 2 — Run agent on payment failures
    # ------------------------------------------------------------------
    pay_results: list[dict] = []
    if payment_events:
        print(f"{sep}")
        print(f"  PROCESSING PAYMENT FAILURES ({len(payment_events)} events)")
        print(sep)
        pay_results = run_batch("payment_failure", payment_events)
        print()

    # ------------------------------------------------------------------
    # Step 3 — Run agent on checkout abandonments
    # ------------------------------------------------------------------
    checkout_results: list[dict] = []
    if checkout_events:
        print(f"{sep}")
        print(f"  PROCESSING CHECKOUT ABANDONMENTS ({len(checkout_events)} events)")
        print(sep)
        checkout_results = run_batch("checkout_abandonment", checkout_events)
        print()

    # ------------------------------------------------------------------
    # Step 4 — Print summary
    # ------------------------------------------------------------------
    all_results = pay_results + checkout_results
    outcomes = Counter(r.get("outcome", "unknown") for r in all_results)
    decisions = Counter(r.get("agent_decision", "unknown") for r in all_results)

    print(f"{sep}")
    print("  OUTCOME SUMMARY")
    print(sep)
    df_outcomes = pd.DataFrame(
        [{"outcome": k, "count": v} for k, v in sorted(outcomes.items())]
    )
    print(df_outcomes.to_string(index=False))

    print(f"\n{sep}")
    print("  DECISION SUMMARY")
    print(sep)
    df_decisions = pd.DataFrame(
        [{"agent_decision": k, "count": v} for k, v in sorted(decisions.items())]
    )
    print(df_decisions.to_string(index=False))

    escalated = sum(1 for r in all_results if r.get("escalated", False))
    errors = sum(1 for r in all_results if r.get("error_detail"))
    recovered = outcomes.get("recovered", 0)

    print(f"\n  Events processed : {len(all_results)}")
    print(f"  Recovered        : {recovered}")
    print(f"  Escalated        : {escalated}")
    print(f"  Errors           : {errors}")
    print(f"\n  Completed: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{sep}\n")

    engine.dispose()


if __name__ == "__main__":
    main()
