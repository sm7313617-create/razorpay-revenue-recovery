"""
integration_test_task5.py
----------------------------------------------------------------
Integration test for Task 5 -- Intervention Executors.

Fetches up to 5 real detected payment failures from the DB,
runs run_batch on them, and prints an outcome distribution table.

!! DB-MUTATING SCRIPT — READ BEFORE RUNNING !!
-----------------------------------------------
This script executes the full live agent pipeline against the real
PostgreSQL database.  Every run inserts rows into:
  - recovery_actions  (one row per event processed)
  - audit_log         (one row per event processed)
  - payments          (may UPDATE status to 'success' when retry succeeds)

Running this script during metrics collection or before a demo will
inflate recovery_actions / audit_log counts and distort all reports.

To restore a clean seed state, run:
    python -m data.reset_db
"""

import os
import sys
from collections import Counter

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

load_dotenv()

engine = create_engine(os.environ["DB_URL"], echo=False, future=True)

from detectors.payment_failure import detect_failed_payments
from agent.graph import run_batch
from audit.logger import get_escalation_report, print_audit_trail

print("\n" + "=" * 65)
print("  TASK 5 INTEGRATION TEST -- Intervention Executors")
print("=" * 65)

with Session(engine) as session:
    events = detect_failed_payments(session)

print(f"\n  Detected {len(events)} unprocessed payment failure(s) in DB.")

if not events:
    print("  No events to process -- seed the DB first.\n")
    sys.exit(0)

batch = events[:5]
print(f"  Running run_batch on first {len(batch)} event(s)...\n")

results = run_batch("payment_failure", batch)

# ------------------------------------------------------------------
# Outcome distribution
# ------------------------------------------------------------------
outcomes = Counter(r.get("outcome", "unknown") for r in results)

print("=" * 65)
print("  OUTCOME DISTRIBUTION")
print("=" * 65)
df_outcomes = pd.DataFrame(
    [{"outcome": k, "count": v} for k, v in sorted(outcomes.items())]
)
print(df_outcomes.to_string(index=False))
print()

# ------------------------------------------------------------------
# Per-event detail
# ------------------------------------------------------------------
print("=" * 65)
print("  PER-EVENT DETAIL")
print("=" * 65)
rows = []
for r in results:
    rows.append({
        "reference_id":  r.get("reference_id", "")[:18] + "...",
        "decision":      r.get("agent_decision", ""),
        "outcome":       r.get("outcome", ""),
        "escalated":     r.get("escalated", False),
        "error_detail":  (r.get("error_detail") or "")[:40],
    })
df_detail = pd.DataFrame(rows)
print(df_detail.to_string(index=False))
print()

# ------------------------------------------------------------------
# Escalation report
# ------------------------------------------------------------------
print("=" * 65)
print("  ESCALATION REPORT")
print("=" * 65)
with Session(engine) as session:
    report = get_escalation_report(session)

print(f"  Total escalated (all time): {report['total_escalated']}")
print(f"  By event type             : {report['escalation_by_event_type']}")
print()

# ------------------------------------------------------------------
# Print audit trail for the first reference
# ------------------------------------------------------------------
if results:
    first_ref = results[0].get("reference_id", "")
    if first_ref:
        print("=" * 65)
        print(f"  AUDIT TRAIL -- first reference ({first_ref[:18]}...)")
        print("=" * 65)
        print_audit_trail(first_ref)

engine.dispose()
print("\nDone.\n")
