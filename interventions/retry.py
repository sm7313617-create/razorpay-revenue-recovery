"""
interventions/retry.py
----------------------------------------------------------------
Intervention executor for the "retry" agent decision.

The Razorpay test environment does not support triggering retries
through the SDK, so outcomes are *simulated* deterministically:

* The random seed is derived from the payment UUID, guaranteeing
  the same payment always produces the same simulated outcome.
* Success probability per failure_code:
    - insufficient_funds  -> 70 %
    - card_declined       -> 40 %
    - gateway_timeout     -> 80 %
    - anything else       -> 50 %

Public API
----------
execute_retry(state, session) -> dict
    Execute (or simulate) a payment retry and update the DB rows.
"""

from __future__ import annotations

import logging
import os
import random
import uuid
from typing import Any

import razorpay
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AuditLog, Payment, RecoveryAction

# ---------------------------------------------------------------------------
# Load environment once at module level
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Razorpay client -- initialised once at module level
# ---------------------------------------------------------------------------

_RAZORPAY_KEY_ID: str = os.environ["RAZORPAY_KEY_ID"]
_RAZORPAY_KEY_SECRET: str = os.environ["RAZORPAY_KEY_SECRET"]

razorpay_client: razorpay.Client = razorpay.Client(
    auth=(_RAZORPAY_KEY_ID, _RAZORPAY_KEY_SECRET)
)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Success-rate table (failure_code -> probability of recovery)
# ---------------------------------------------------------------------------

_SUCCESS_RATES: dict[str, float] = {
    "insufficient_funds": 0.70,
    "card_declined":      0.40,
    "gateway_timeout":    0.80,
}
_DEFAULT_SUCCESS_RATE: float = 0.50


def _simulate_retry(payment_uuid: uuid.UUID, failure_code: str | None) -> bool:
    """Return True if the simulated retry succeeds.

    Uses a seeded RNG so that the same payment UUID always yields the same
    outcome -- deterministic and reproducible across test runs.

    Args:
        payment_uuid: The UUID of the payment being retried.
        failure_code: The failure code stored on the payment row.

    Returns:
        True if the simulated retry recovers the payment, False otherwise.
    """
    rng = random.Random(payment_uuid.int)
    success_rate = _SUCCESS_RATES.get(failure_code or "", _DEFAULT_SUCCESS_RATE)
    return rng.random() < success_rate


def _latest_recovery_action(
    session: Session, payment_uuid: uuid.UUID
) -> RecoveryAction | None:
    """Return the most recently created RecoveryAction for *payment_uuid*.

    Args:
        session:      Active SQLAlchemy session.
        payment_uuid: UUID of the payment / reference entity.

    Returns:
        The most recent RecoveryAction ORM instance, or None.
    """
    return session.scalar(
        select(RecoveryAction)
        .where(RecoveryAction.reference_id == payment_uuid)
        .order_by(RecoveryAction.created_at.desc())
        .limit(1)
    )


def _update_audit_outcome(
    session: Session, reference_id: str, outcome: str
) -> None:
    """Update the outcome field of the latest AuditLog row for *reference_id*.

    Targets the most-recently-inserted row for this reference to reflect the
    true execution outcome (the row was written with outcome="pending" by
    log_audit_entry).

    Args:
        session:      Active SQLAlchemy session.
        reference_id: String UUID of the reference entity.
        outcome:      Final outcome string to persist.
    """
    try:
        ref_uuid = uuid.UUID(reference_id)
        audit_row: AuditLog | None = session.scalar(
            select(AuditLog)
            .where(AuditLog.reference_id == ref_uuid)
            .order_by(AuditLog.timestamp.desc())
            .limit(1)
        )
        if audit_row is not None:
            audit_row.outcome = outcome
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update audit_log outcome: %s", exc)


def execute_retry(state: dict[str, Any], session: Session) -> dict[str, Any]:
    """Execute a payment retry intervention and update the database.

    Flow
    ----
    1. Read backoff_seconds and max_retries from state["action_params"].
    2. Call razorpay_client.payment.fetch(reference_id) to confirm the
       payment exists in the gateway (test mode -- no actual charge is made).
    3. Simulate the retry outcome deterministically using a seeded RNG keyed
       on the payment UUID.
    4. On simulated success:
       - Set payments.status = "success"
       - Set recovery_actions.status = "success"
       - Set state["outcome"] = "recovered"
    5. On simulated failure:
       - Set recovery_actions.status = "failed"
       - Set state["outcome"] = "retry_failed"
    6. On any exception:
       - Set recovery_actions.status = "failed"
       - Set state["outcome"] = "error"
       - Set state["error_detail"] = str(e)
    7. Update audit_log.outcome to match the final outcome.
    8. A single session.commit() is issued after all mutations.

    Args:
        state:   Agent state dict flowing through the LangGraph graph.
        session: Active SQLAlchemy session shared with the caller.

    Returns:
        Updated state dict with outcome (and optionally error_detail) set.
    """
    reference_id: str = state["reference_id"]
    action_params: dict[str, Any] = state.get("action_params", {})

    _backoff_seconds: list[int] = action_params.get("backoff_seconds", [300, 900, 3600])
    _max_retries: int = action_params.get("max_retries", 3)

    outcome: str
    error_detail: str | None = None

    try:
        payment_uuid = uuid.UUID(reference_id)

        # 1. Fetch payment details from Razorpay (test mode -- read-only)
        rzp_payment_id = f"pay_{reference_id.replace('-', '')[:14]}"
        try:
            razorpay_client.payment.fetch(rzp_payment_id)
            logger.info("Razorpay fetch succeeded for %s", rzp_payment_id)
        except Exception as fetch_exc:  # noqa: BLE001
            logger.debug(
                "Razorpay fetch error (expected in test mode): %s", fetch_exc
            )

        # 2. Determine failure_code from the DB payment row
        payment_row: Payment | None = session.scalar(
            select(Payment).where(Payment.id == payment_uuid)
        )
        failure_code: str | None = payment_row.failure_code if payment_row else None

        # 3. Simulate the retry outcome
        success = _simulate_retry(payment_uuid, failure_code)

        if success:
            if payment_row is not None:
                payment_row.status = "success"

            recovery_row = _latest_recovery_action(session, payment_uuid)
            if recovery_row is not None:
                recovery_row.status = "success"

            outcome = "recovered"
            logger.info(
                "Retry simulated SUCCESS for payment %s (failure_code=%s)",
                reference_id,
                failure_code,
            )

        else:
            recovery_row = _latest_recovery_action(session, payment_uuid)
            if recovery_row is not None:
                recovery_row.status = "failed"

            outcome = "retry_failed"
            logger.info(
                "Retry simulated FAILURE for payment %s (failure_code=%s)",
                reference_id,
                failure_code,
            )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during retry execution: %s", exc)
        outcome = "error"
        error_detail = str(exc)

        try:
            ref_uuid = uuid.UUID(reference_id)
            recovery_row = _latest_recovery_action(session, ref_uuid)
            if recovery_row is not None:
                recovery_row.status = "failed"
        except Exception:  # noqa: BLE001
            pass

    # 4. Update the audit_log outcome
    _update_audit_outcome(session, reference_id, outcome)

    # 5. Single atomic commit
    session.commit()

    updated_state = {**state, "outcome": outcome}
    if error_detail is not None:
        updated_state["error_detail"] = error_detail

    return updated_state
