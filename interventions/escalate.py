"""
interventions/escalate.py
----------------------------------------------------------------
Intervention executor for the "escalate" agent decision.

This is a terminal node.  Once escalation is triggered the agent
MUST NOT schedule any further automated retries.  A structured
record is printed to stdout so that an on-call system or webhook
could consume the output in a production deployment.

Public API
----------
execute_escalate(state, session) -> dict
    Log the escalation record, update DB rows, and return the
    final state with outcome="escalated".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog, RecoveryAction

# ---------------------------------------------------------------------------
# Load environment once at module level
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _latest_recovery_action(
    session: Session, ref_uuid: uuid.UUID
) -> RecoveryAction | None:
    """Return the most recently created RecoveryAction for *ref_uuid*.

    Args:
        session:  Active SQLAlchemy session.
        ref_uuid: UUID of the payment or checkout session.

    Returns:
        The most recent RecoveryAction ORM instance, or None.
    """
    return session.scalar(
        select(RecoveryAction)
        .where(RecoveryAction.reference_id == ref_uuid)
        .order_by(RecoveryAction.created_at.desc())
        .limit(1)
    )


def _update_audit_for_escalation(
    session: Session, ref_uuid: uuid.UUID
) -> None:
    """Set outcome="escalated" and escalated=True on the latest AuditLog row.

    Args:
        session:  Active SQLAlchemy session.
        ref_uuid: UUID of the reference entity.
    """
    audit_row: AuditLog | None = session.scalar(
        select(AuditLog)
        .where(AuditLog.reference_id == ref_uuid)
        .order_by(AuditLog.timestamp.desc())
        .limit(1)
    )
    if audit_row is not None:
        audit_row.outcome = "escalated"
        audit_row.escalated = True


# ---------------------------------------------------------------------------
# Public executor
# ---------------------------------------------------------------------------


def execute_escalate(state: dict[str, Any], session: Session) -> dict[str, Any]:
    """Perform a human-handoff escalation and update the database.

    This is a terminal intervention node.  After escalation no further
    automated action should be attempted for this reference_id.

    Flow
    ----
    1. Build a structured escalation record from the current state.
    2. Print the record to stdout (production: replace with queue/webhook).
    3. Set recovery_actions.status = "escalated" on the latest row.
    4. Set audit_log.outcome = "escalated", audit_log.escalated = True.
    5. Issue a single session.commit().
    6. Return state with outcome="escalated", escalated=True.

    Args:
        state:   Agent state dict flowing through the LangGraph graph.
        session: Active SQLAlchemy session shared with the caller.

    Returns:
        Updated state dict with outcome="escalated" and escalated=True.
    """
    reference_id: str = state["reference_id"]
    event_type: str = state["event_type"]
    event_data: dict[str, Any] = state.get("event_data", {})
    attempt_count: int = state.get("attempt_count", 0)
    action_params: dict[str, Any] = state.get("action_params", {})
    error_detail: str | None = state.get("error_detail")

    # Derive the most useful context field for the escalation record
    original_code_or_priority: str = (
        str(event_data.get("failure_code", ""))
        or str(event_data.get("recovery_priority", ""))
        or "unknown"
    )

    reason: str = action_params.get(
        "reason",
        "Automated recovery exhausted or not applicable -- escalating to human review.",
    )

    # ------------------------------------------------------------------
    # Build and emit the structured escalation record
    # ------------------------------------------------------------------
    escalation_record: dict[str, Any] = {
        "timestamp":                      datetime.now(timezone.utc).isoformat(),
        "event_type":                     event_type,
        "reference_id":                   reference_id,
        "reason":                         reason,
        "attempt_count":                  attempt_count,
        "original_failure_code_or_priority": original_code_or_priority,
        "requires_human":                 True,
    }
    if error_detail:
        escalation_record["error_detail"] = error_detail

    record_json = json.dumps(escalation_record, indent=2, ensure_ascii=False)
    print(f"\n[ESCALATION] {record_json}\n")
    logger.info("Human escalation triggered: %s", escalation_record)

    # ------------------------------------------------------------------
    # Update recovery_actions
    # ------------------------------------------------------------------
    ref_uuid = uuid.UUID(reference_id)
    recovery_row = _latest_recovery_action(session, ref_uuid)
    if recovery_row is not None:
        recovery_row.status = "escalated"

    # ------------------------------------------------------------------
    # Update audit_log
    # ------------------------------------------------------------------
    _update_audit_for_escalation(session, ref_uuid)

    # ------------------------------------------------------------------
    # Single atomic commit
    # ------------------------------------------------------------------
    session.commit()

    return {
        **state,
        "outcome":  "escalated",
        "escalated": True,
    }
