"""
agent/nodes.py
----------------------------------------------------------------
LangGraph node functions and AgentState schema for the Razorpay
AI Revenue Recovery agent.

State schema
------------
AgentState  (TypedDict) — the single shared state dict flowing through
            every node in the graph.

Nodes (plain functions, state-in → state-out)
---------------------------------------------
1. check_stopping_rules   — deterministic guardrails, no LLM
2. decide_intervention    — Gemini-backed classification
3. prepare_action         — maps decision → structured action_params
4. log_audit_entry        — persists one row to audit_log
5. write_recovery_action  — persists one row to recovery_actions
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from typing_extensions import TypedDict

from agent.prompts import CHECKOUT_ABANDONMENT_PROMPT, PAYMENT_FAILURE_PROMPT
from db.models import AuditLog, RecoveryAction

# ---------------------------------------------------------------------------
# Load environment once at module level (no mutable global state)
# ---------------------------------------------------------------------------

load_dotenv()


# ---------------------------------------------------------------------------
# AgentState schema
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    """Shared state dict that flows through every node in the graph.

    Fields
    ------
    event_type     : "payment_failure" or "checkout_abandonment"
    reference_id   : UUID of the payment or checkout session (as str)
    event_data     : Full detected event dict from the detector
    attempt_count  : Count of existing recovery_actions for this reference_id
    agent_decision : Filled by decide_intervention (or check_stopping_rules)
    action_params  : Filled by prepare_action
    outcome        : Filled by execute_action / log step ("pending" initially)
    escalated      : True when the event is being escalated to human review
    error_detail   : Non-None when an exception occurred in a node
    """

    event_type: str
    reference_id: str
    event_data: dict
    attempt_count: int
    agent_decision: str
    action_params: dict
    outcome: str
    escalated: bool
    error_detail: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_session() -> tuple[Any, Session]:
    """Create a fresh SQLAlchemy engine + session for one run.

    Returns a (engine, session) tuple so the caller can dispose the engine
    after committing.  Never reuses a cached engine — satisfies the no-global-
    mutable-state requirement.
    """
    db_url = os.environ["DB_URL"]
    engine = create_engine(db_url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, SessionLocal()


def _serialize_event_data(event_data: dict) -> dict:
    """Return a JSON-safe copy of event_data (UUID → str, Decimal → str)."""
    safe: dict = {}
    for k, v in event_data.items():
        if isinstance(v, uuid.UUID):
            safe[k] = str(v)
        elif hasattr(v, "__float__"):          # Decimal, float, int
            safe[k] = float(v)
        elif isinstance(v, datetime):
            safe[k] = v.isoformat()
        else:
            safe[k] = v
    return safe


# ---------------------------------------------------------------------------
# Node 1 — check_stopping_rules
# ---------------------------------------------------------------------------


def check_stopping_rules(state: AgentState) -> AgentState:
    """Apply deterministic guardrails before any LLM call.

    Rules (evaluated in order — first match wins):

    1. attempt_count >= 3
       → agent_decision = "escalate", escalated = True

    2. event_type == "payment_failure" AND failure_code == "bank_downtime"
       → agent_decision = "notify_then_escalate", escalated = True

    3. event_type == "checkout_abandonment" AND
       minutes_since_abandonment > 120
       → agent_decision = "notify_only"

    If none of the rules match, state is returned unchanged so that the
    conditional edge in the graph routes to decide_intervention.

    Args:
        state: Current AgentState.

    Returns:
        Updated AgentState (or the original if no rule matched).
    """
    attempt_count = state["attempt_count"]
    event_type = state["event_type"]
    event_data = state["event_data"]

    # Rule 1 — too many prior attempts
    if attempt_count >= 3:
        return {
            **state,
            "agent_decision": "escalate",
            "escalated": True,
        }

    # Rule 2 — bank downtime is not retryable
    if event_type == "payment_failure":
        failure_code = event_data.get("failure_code", "")
        if failure_code == "bank_downtime":
            return {
                **state,
                "agent_decision": "notify_then_escalate",
                "escalated": True,
            }

    # Rule 3 — very stale abandonment — only low-touch notification
    if event_type == "checkout_abandonment":
        minutes = float(event_data.get("minutes_since_abandonment", 0))
        if minutes > 120:
            return {
                **state,
                "agent_decision": "notify_only",
            }

    return state


# ---------------------------------------------------------------------------
# Node 2 — decide_intervention
# ---------------------------------------------------------------------------


def decide_intervention(state: AgentState) -> AgentState:
    """Call Gemini to classify the best recovery intervention.

    Selects the appropriate prompt template from agent/prompts.py based on
    event_type, formats it with the event data, invokes
    ChatGoogleGenerativeAI(model="gemini-1.5-flash"), and stores the
    stripped, lower-cased single-word response in agent_decision.

    On any exception the node degrades gracefully:
      - agent_decision is set to "escalate"
      - error_detail is set to str(e)

    Args:
        state: Current AgentState (agent_decision must be empty string here).

    Returns:
        Updated AgentState with agent_decision (and optionally error_detail).
    """
    event_type = state["event_type"]
    event_data = state["event_data"]

    try:
        llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash")

        if event_type == "payment_failure":
            prompt = PAYMENT_FAILURE_PROMPT.format(
                failure_code=event_data.get("failure_code", "unknown"),
                amount=float(event_data.get("amount", 0)),
                severity=event_data.get("severity", "low"),
                merchant_id=event_data.get("merchant_id", "unknown"),
                attempt_count=state["attempt_count"],
            )
        else:  # checkout_abandonment
            prompt = CHECKOUT_ABANDONMENT_PROMPT.format(
                cart_value=float(event_data.get("cart_value", 0)),
                minutes_since_abandonment=event_data.get(
                    "minutes_since_abandonment", 0
                ),
                recovery_priority=event_data.get("recovery_priority", "low"),
                merchant_id=event_data.get("merchant_id", "unknown"),
            )

        response = llm.invoke(prompt)

        # gemini-3.6-flash returns content as a list of part-dicts:
        # [{'type': 'text', 'text': 'retry', 'extras': {...}}]
        # Older models return a plain str.  Handle both shapes robustly.
        raw_content = response.content
        if isinstance(raw_content, list):
            # Extract 'text' from the first text-type part
            text_parts = [
                p["text"] for p in raw_content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            raw_text = text_parts[0] if text_parts else str(raw_content)
        else:
            raw_text = str(raw_content)

        decision = raw_text.strip().lower()

        return {**state, "agent_decision": decision}

    except Exception as exc:  # noqa: BLE001
        return {
            **state,
            "agent_decision": "escalate",
            "error_detail": str(exc),
        }


# ---------------------------------------------------------------------------
# Node 3 — prepare_action
# ---------------------------------------------------------------------------


def prepare_action(state: AgentState) -> AgentState:
    """Map agent_decision to a structured action_params dict.

    Decision → action_params mapping:

    retry
        max_retries    : 3
        backoff_seconds: [300, 900, 3600]
        reason         : human-readable string

    notify / notify_only / notify_then_escalate
        channel        : "email"
        template       : event-type-specific template name
        reason         : human-readable string

    discount
        percent        : 10 if cart_value >= 5000 else 15
        valid_hours    : 24
        reason         : human-readable string

    escalate (or any unrecognised decision)
        reason         : human-readable string
        requires_human : True

    Args:
        state: Current AgentState with agent_decision populated.

    Returns:
        Updated AgentState with action_params populated.
    """
    decision = state["agent_decision"]
    event_data = state["event_data"]

    if decision == "retry":
        params: dict[str, Any] = {
            "max_retries": 3,
            "backoff_seconds": [300, 900, 3600],
            "reason": (
                "Transient failure detected — scheduling automated retry "
                "with exponential backoff."
            ),
        }

    elif decision in ("notify", "notify_only", "notify_then_escalate"):
        event_type = state["event_type"]
        template = (
            "payment_failed"
            if event_type == "payment_failure"
            else "checkout_abandoned"
        )
        params = {
            "channel": "email",
            "template": template,
            "reason": (
                "Automated recovery is not advisable — notifying customer "
                "and/or merchant via email."
            ),
        }

    elif decision == "discount":
        cart_value = float(event_data.get("cart_value", 0))
        discount_percent = 10 if cart_value >= 5000 else 15
        params = {
            "percent": discount_percent,
            "valid_hours": 24,
            "reason": (
                f"Offering a {discount_percent}% discount to recover "
                "abandoned cart value."
            ),
        }

    else:  # "escalate" or any unexpected value
        params = {
            "reason": (
                "Automated recovery exhausted or not applicable — "
                "escalating to human review."
            ),
            "requires_human": True,
        }

    return {**state, "action_params": params}


# ---------------------------------------------------------------------------
# Node 4 — log_audit_entry
# ---------------------------------------------------------------------------


def log_audit_entry(state: AgentState) -> AgentState:
    """Write one immutable row to the audit_log table.

    Captures the full agent context at this point in the pipeline:
      - timestamp    : UTC now
      - event_type   : from state
      - reference_id : UUID parsed from state["reference_id"]
      - agent_state  : "EXECUTING"
      - decision_made: agent_decision
      - action_taken : JSON-serialised action_params summary
      - outcome      : "pending" (execution happens downstream)
      - escalated    : from state
      - error_detail : from state (may be None)
      - raw_context  : full event_data as a JSON-safe dict

    A fresh engine and session are created per call to avoid shared state.
    The session is always closed in the finally block.

    Args:
        state: Current AgentState.

    Returns:
        State unchanged (audit logging is a side-effect node).
    """
    engine, session = _make_session()
    try:
        action_taken_str = json.dumps(
            {k: str(v) for k, v in state["action_params"].items()},
            ensure_ascii=False,
        )

        reference_uuid = (
            uuid.UUID(state["reference_id"])
            if state["reference_id"]
            else None
        )

        entry = AuditLog(
            timestamp=datetime.now(timezone.utc),
            event_type=state["event_type"],
            reference_id=reference_uuid,
            agent_state="EXECUTING",
            decision_made=state["agent_decision"],
            action_taken=action_taken_str[:128],   # column is VARCHAR(128)
            outcome=state.get("outcome", "pending"),
            escalated=state["escalated"],
            error_detail=state.get("error_detail"),
            raw_context=_serialize_event_data(state["event_data"]),
        )

        session.add(entry)
        session.commit()
    finally:
        session.close()
        engine.dispose()

    return state


# ---------------------------------------------------------------------------
# Node 5 — write_recovery_action
# ---------------------------------------------------------------------------


def write_recovery_action(state: AgentState) -> AgentState:
    """Write one row to the recovery_actions table.

    Stores:
      - event_type   : from state
      - reference_id : UUID parsed from state["reference_id"]
      - action_taken : agent_decision (must match the RecoveryActionTaken enum)
      - action_params: full action_params dict as JSON
      - status       : "pending"

    agent_decision values produced by stopping rules
    (notify_then_escalate, notify_only) are normalised to valid enum
    values before writing:
      notify_then_escalate → "escalate"
      notify_only          → "notify"

    A fresh engine and session are created per call.

    Args:
        state: Current AgentState.

    Returns:
        Updated AgentState with outcome="pending" set.
    """
    # Normalise extended stopping-rule decisions to valid enum values
    _DECISION_MAP: dict[str, str] = {
        "notify_then_escalate": "escalate",
        "notify_only": "notify",
    }
    raw_decision = state["agent_decision"]
    db_action = _DECISION_MAP.get(raw_decision, raw_decision)

    # Guard: if the decision is still not a valid enum value, fall back to escalate
    valid_actions = {"retry", "notify", "discount", "escalate"}
    if db_action not in valid_actions:
        db_action = "escalate"

    engine, session = _make_session()
    try:
        record = RecoveryAction(
            event_type=state["event_type"],
            reference_id=uuid.UUID(state["reference_id"]),
            action_taken=db_action,
            action_params=state["action_params"],
            status="pending",
        )

        session.add(record)
        session.commit()
    finally:
        session.close()
        engine.dispose()

    return {**state, "outcome": "pending"}
