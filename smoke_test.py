import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

load_dotenv()
engine = create_engine(os.getenv("DB_URL"))

from detectors.payment_failure import detect_failed_payments
from detectors.checkout_abandonment import detect_abandoned_checkouts
from agent.graph import run_agent

with Session(engine) as session:
    # --- TEST 1: payment failure that FORCES Gemini call ---
    failed = detect_failed_payments(session)
    non_stopping = [p for p in failed
                    if p["failure_code"] not in ("bank_downtime",)]

    if non_stopping:
        test_payment = non_stopping[0]
        print(f"\n=== TEST 1: Gemini live call (payment_failure) ===")
        print(f"failure_code : {test_payment['failure_code']}")
        print(f"amount       : {test_payment['amount']}")
        result1 = run_agent("payment_failure", test_payment)
        print(f"agent_decision : {result1['agent_decision']}")
        print(f"escalated      : {result1['escalated']}")
        print(f"error_detail   : {result1['error_detail']}")
        print(f"outcome        : {result1['outcome']}")
    else:
        print("No non-stopping payment events found — check seed data")

    # --- TEST 2: checkout abandonment end-to-end ---
    # Sort ascending by minutes_since_abandonment so the freshest
    # (most recently abandoned) session is first — this ensures we
    # pick the row inserted for smoke-testing (45 min old) rather than
    # the stale seed rows (>120 min old) that would hit the stopping rule.
    abandoned = detect_abandoned_checkouts(session)
    abandoned_sorted = sorted(abandoned, key=lambda x: x["minutes_since_abandonment"])
    if abandoned_sorted:
        test_checkout = abandoned_sorted[0]
        print(f"\n=== TEST 2: Checkout abandonment end-to-end ===")
        print(f"cart_value              : {test_checkout['cart_value']}")
        print(f"minutes_since_abandon   : {test_checkout['minutes_since_abandonment']}")
        print(f"recovery_priority       : {test_checkout['recovery_priority']}")
        result2 = run_agent("checkout_abandonment", test_checkout)
        print(f"agent_decision : {result2['agent_decision']}")
        print(f"escalated      : {result2['escalated']}")
        print(f"error_detail   : {result2['error_detail']}")
        print(f"outcome        : {result2['outcome']}")
    else:
        print("No abandoned checkouts found — check seed data")

print("\n=== Verify DB rows ===")
print("Check pgAdmin: audit_log and recovery_actions should now have 3 rows each")
