"""
verify_escalated_flag.py
Unit-level check: when Gemini returns "escalate" and
check_stopping_rules did NOT set escalated, prepare_action must flip it True.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from agent.nodes import prepare_action, AgentState

# Simulate state where Gemini returned "escalate" but escalated is still False
state: AgentState = {
    "event_type": "payment_failure",
    "reference_id": "00000000-0000-0000-0000-000000000001",
    "event_data": {
        "failure_code": "card_declined",
        "amount": 500,
        "severity": "medium",
        "merchant_id": "MID_X",
        "customer_id": "CUS_X",
    },
    "attempt_count": 1,
    "agent_decision": "escalate",   # Gemini returned this
    "action_params": {},
    "outcome": "",
    "escalated": False,             # was NOT set by stopping rule
    "error_detail": None,
}

result = prepare_action(state)
print(f"agent_decision : {result['agent_decision']}")
print(f"escalated      : {result['escalated']}")
print(f"action_params  : {result['action_params']}")

assert result["escalated"] is True, "BUG: escalated is still False when decision==escalate!"
print()
print("PASS — escalated is True when Gemini decision is escalate")
