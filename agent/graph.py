"""
agent/graph.py
----------------------------------------------------------------
LangGraph StateGraph definition for the Razorpay AI Revenue Recovery agent.

Graph topology
--------------
Entry
  └─► check_stopping_rules
        ├─► (if agent_decision already set) prepare_action
        └─► (else) decide_intervention ──► prepare_action
                                              └─► log_audit_entry
                                                    └─► write_recovery_action
                                                          └─► END

Public API
----------
run_agent(event_type, event_data) -> dict
    Runs the full graph for a single event and returns the final AgentState.

run_batch(event_type, events) -> list[dict]
    Calls run_agent for every event in the list and returns all final states.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agent.nodes import (
    AgentState,
    check_stopping_rules,
    decide_intervention,
    log_audit_entry,
    prepare_action,
    write_recovery_action,
)
from db.models import RecoveryAction

# ---------------------------------------------------------------------------
# Load environment once at module import time
# ---------------------------------------------------------------------------

load_dotenv()


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _build_graph() -> Any:
    """Construct and compile the LangGraph StateGraph.

    The graph is compiled once at module import time and reused across all
    run_agent / run_batch calls.  The graph itself holds no mutable state
    between runs — all state lives in the AgentState dict created fresh per
    run_agent call.

    Returns:
        A compiled LangGraph runnable (CompiledGraph).
    """
    graph = StateGraph(AgentState)

    # Register every node
    graph.add_node("check_stopping_rules", check_stopping_rules)
    graph.add_node("decide_intervention", decide_intervention)
    graph.add_node("prepare_action", prepare_action)
    graph.add_node("log_audit_entry", log_audit_entry)
    graph.add_node("write_recovery_action", write_recovery_action)

    # Entry point
    graph.set_entry_point("check_stopping_rules")

    # Conditional edge: skip LLM if stopping rule already made a decision
    def _route_after_stopping_rules(state: AgentState) -> str:
        """Route to decide_intervention or skip directly to prepare_action."""
        if state.get("agent_decision"):
            return "prepare_action"
        return "decide_intervention"

    graph.add_conditional_edges(
        "check_stopping_rules",
        _route_after_stopping_rules,
        {
            "prepare_action": "prepare_action",
            "decide_intervention": "decide_intervention",
        },
    )

    # Linear edges for the remainder of the pipeline
    graph.add_edge("decide_intervention", "prepare_action")
    graph.add_edge("prepare_action", "log_audit_entry")
    graph.add_edge("log_audit_entry", "write_recovery_action")
    graph.add_edge("write_recovery_action", END)

    return graph.compile()


# Compile once — the compiled graph is stateless and safe to reuse
_COMPILED_GRAPH = _build_graph()


# ---------------------------------------------------------------------------
# Helper — count prior recovery attempts
# ---------------------------------------------------------------------------


def _count_attempts(reference_id: str) -> int:
    """Return the number of existing RecoveryAction rows for *reference_id*.

    Creates a fresh engine + session per call so no global connection state
    is retained between runs.

    Args:
        reference_id: String representation of the UUID.

    Returns:
        Integer count (0 if none found or reference_id is invalid).
    """
    try:
        ref_uuid = uuid.UUID(reference_id)
    except (ValueError, AttributeError):
        return 0

    db_url = os.environ["DB_URL"]
    engine = create_engine(db_url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    try:
        with SessionLocal() as session:
            stmt = select(func.count()).where(
                RecoveryAction.reference_id == ref_uuid
            )
            count: int = session.scalar(stmt) or 0
    finally:
        engine.dispose()

    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_agent(event_type: str, event_data: dict) -> dict:
    """Run the full recovery graph for a single detected event.

    Builds an initial AgentState from the event, queries the DB for the
    prior attempt count, compiles (reuses) the graph, invokes it, and
    returns the final state dict.

    Args:
        event_type : "payment_failure" or "checkout_abandonment".
        event_data : Full event dict as returned by a detector function.

    Returns:
        Final AgentState dict after all nodes have executed.
    """
    # Resolve the reference_id from whichever key the detector uses
    raw_ref = event_data.get("payment_id") or event_data.get("session_id")
    reference_id: str = str(raw_ref) if raw_ref is not None else str(uuid.uuid4())

    attempt_count = _count_attempts(reference_id)

    initial_state: AgentState = {
        "event_type": event_type,
        "reference_id": reference_id,
        "event_data": event_data,
        "attempt_count": attempt_count,
        "agent_decision": "",
        "action_params": {},
        "outcome": "",
        "escalated": False,
        "error_detail": None,
    }

    final_state: dict = _COMPILED_GRAPH.invoke(initial_state)
    return final_state


def run_batch(event_type: str, events: list[dict]) -> list[dict]:
    """Run run_agent for every event in *events* and return all final states.

    Events are processed sequentially.  Each call to run_agent creates its
    own fresh engine/session and is fully isolated.

    Args:
        event_type: "payment_failure" or "checkout_abandonment".
        events    : List of event dicts from a detector function.

    Returns:
        List of final AgentState dicts, one per input event, in order.
    """
    return [run_agent(event_type, event) for event in events]
