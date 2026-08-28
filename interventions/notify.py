"""
interventions/notify.py
----------------------------------------------------------------
Intervention executor for the "notify", "notify_only", and
"notify_then_escalate" agent decisions.

No real notification is dispatched.  A structured record is
printed to stdout and written to the Python logger so that
integration tests and CI pipelines can assert on the output.

Public API
----------
execute_notify(state, session) -> dict
    Log a mock notification, optionally mark as escalated, and
    update recovery_actions + audit_log in one atomic commit.
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

from db.models import AuditLog, CheckoutSession, Payment, RecoveryAction

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


def _update_audit_outcome(
    session: Session,
    ref_uuid: uuid.UUID,
    outcome: str,
    escalated: bool,
) -> None:
    """Update outcome (and escalated flag) on the latest AuditLog row.

    Args:
        session:   Active SQLAlchemy session.
        ref_uuid:  UUID of the reference entity.
        outcome:   Final outcome string to persist.
        escalated: Whether to mark the audit row as escalated.
    """
    audit_row: AuditLog | None = session.scalar(
        select(AuditLog)
        .where(AuditLog.reference_id == ref_uuid)
        .order_by(AuditLog.timestamp.desc())
        .limit(1)
    )
    if audit_row is not None:
        audit_row.outcome = outcome
        audit_row.escalated = escalated


# ---------------------------------------------------------------------------
# Public executor
# ---------------------------------------------------------------------------


def execute_notify(state: dict[str, Any], session: Session) -> dict[str, Any]:
    """Log a mock customer / merchant notification and update the database.

    Behaviour
    ---------
    - Reads channel and template from state["action_params"].
    - Derives the recipient (customer_id) and amount / cart_value from
      the underlying Payment or CheckoutSession row.
    - Prints and logs a structured notification record (no real email/SMS sent).
    - For "notify_then_escalate": logs the notification *and* marks the
      recovery action + audit row as escalated.
    - Sets recovery_actions.status = "success" (notification "delivered").
    - Sets state["outcome"] = "notified" or "notified_escalated".
    - Issues a single session.commit() after all mutations.

    Args:
        state:   Agent state dict flowing through the LangGraph graph.
        session: Active SQLAlchemy session shared with the caller.

    Returns:
        Updated state dict with outcome set.
    """
    reference_id: str = state["reference_id"]
    event_type: str = state["event_type"]
    agent_decision: str = state.get("agent_decision", "notify")
    action_params: dict[str, Any] = state.get("action_params", {})

    channel: str = action_params.get("channel", "email")
    template: str = action_params.get("template", "generic")

    ref_uuid = uuid.UUID(reference_id)

    # ------------------------------------------------------------------
    # Resolve recipient details from DB
    # ------------------------------------------------------------------
    customer_id: str = "unknown"
    amount_or_value: float = 0.0
    entity_label: str = "payment_id"

    if event_type == "payment_failure":
        payment_row: Payment | None = session.scalar(
            select(Payment).where(Payment.id == ref_uuid)
        )
        if payment_row is not None:
            customer_id = payment_row.customer_id
            amount_or_value = float(payment_row.amount)
            entity_label = "payment_id"
    else:  # checkout_abandonment
        session_row: CheckoutSession | None = session.scalar(
            select(CheckoutSession).where(CheckoutSession.id == ref_uuid)
        )
        if session_row is not None:
            customer_id = session_row.customer_id
            amount_or_value = float(session_row.cart_value)
            entity_label = "session_id"

    # ------------------------------------------------------------------
    # Build the structured notification record
    # ------------------------------------------------------------------
    is_escalated = agent_decision == "notify_then_escalate"

    notification_record: dict[str, Any] = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "channel":     channel,
        "recipient":   customer_id,
        "template":    template,
        entity_label:  reference_id,
        ("amount" if event_type == "payment_failure" else "cart_value"): amount_or_value,
        "escalating":  is_escalated,
    }

    # ------------------------------------------------------------------
    # Print + log (mock dispatch — nothing is actually sent)
    # ------------------------------------------------------------------
    record_json = json.dumps(notification_record, indent=2, ensure_ascii=False)
    print(f"\n[MOCK NOTIFY] {record_json}\n")
    logger.info("Mock notification dispatched: %s", notification_record)

    if is_escalated:
        print(
            f"[MOCK NOTIFY] Escalation triggered after notification "
            f"for reference {reference_id}"
        )
        logger.info(
            "notify_then_escalate: logging escalation for reference %s",
            reference_id,
        )

    # ------------------------------------------------------------------
    # Determine outcome
    # ------------------------------------------------------------------
    outcome = "notified_escalated" if is_escalated else "notified"

    # ------------------------------------------------------------------
    # Update recovery_actions row
    # ------------------------------------------------------------------
    recovery_row = _latest_recovery_action(session, ref_uuid)
    if recovery_row is not None:
        recovery_row.status = "success"

    # ------------------------------------------------------------------
    # Update audit_log row
    # ------------------------------------------------------------------
    _update_audit_outcome(session, ref_uuid, outcome, escalated=is_escalated)

    # ------------------------------------------------------------------
    # Single atomic commit
    # ------------------------------------------------------------------
    session.commit()

    return {
        **state,
        "outcome": outcome,
        "escalated": state.get("escalated", False) or is_escalated,
    }
