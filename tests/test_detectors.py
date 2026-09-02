"""
tests/test_detectors.py
----------------------------------------------------------------
Unit tests for:
  - detectors/payment_failure.py
  - detectors/checkout_abandonment.py

All tests use the in-memory SQLite session fixture from conftest.py.
No production database is touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from detectors.payment_failure import (
    detect_failed_payments,
    get_failure_summary,
    _derive_severity,
)
from detectors.checkout_abandonment import (
    detect_abandoned_checkouts,
    get_abandonment_summary,
    _derive_priority,
)

# Import ORM models — conftest has already patched UUID before these load
from db.models import Payment, CheckoutSession, RecoveryAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payment(
    status: str = "failed",
    failure_code: str | None = "card_declined",
    amount: float = 1000.0,
    merchant_id: str = "m1",
    customer_id: str = "c1",
) -> Payment:
    """Return an unsaved Payment ORM object with sensible defaults."""
    return Payment(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        customer_id=customer_id,
        amount=Decimal(str(amount)),
        currency="INR",
        status=status,
        failure_code=failure_code,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_checkout(
    status: str = "abandoned",
    cart_value: float = 3000.0,
    merchant_id: str = "m1",
    customer_id: str = "c1",
    minutes_ago: float = 30.0,
) -> CheckoutSession:
    """Return an unsaved CheckoutSession ORM object with sensible defaults."""
    from datetime import timedelta
    abandoned_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return CheckoutSession(
        id=uuid.uuid4(),
        merchant_id=merchant_id,
        customer_id=customer_id,
        cart_value=Decimal(str(cart_value)),
        status=status,
        abandoned_at=abandoned_at if status == "abandoned" else None,
        created_at=datetime.now(timezone.utc),
    )


def _make_recovery_action(
    reference_id: uuid.UUID,
    event_type: str,
) -> RecoveryAction:
    """Return an unsaved RecoveryAction pointing to *reference_id*."""
    return RecoveryAction(
        id=uuid.uuid4(),
        event_type=event_type,
        reference_id=reference_id,
        action_taken="retry",
        action_params={},
        status="pending",
        created_at=datetime.now(timezone.utc),
    )


# ===========================================================================
# Payment failure detector tests
# ===========================================================================


def test_detect_failed_payments_returns_only_failed(session):
    """Verify that detect_failed_payments returns only payments with status='failed'.

    Seeds 1 successful payment and 2 failed payments; asserts that exactly
    2 records are returned (the success is excluded).
    """
    p_success = _make_payment(status="success", failure_code=None)
    p_failed1 = _make_payment(status="failed", failure_code="card_declined")
    p_failed2 = _make_payment(status="failed", failure_code="insufficient_funds")
    session.add_all([p_success, p_failed1, p_failed2])
    session.flush()

    result = detect_failed_payments(session)

    assert len(result) == 2
    returned_ids = {r["payment_id"] for r in result}
    assert p_failed1.id in returned_ids
    assert p_failed2.id in returned_ids
    assert p_success.id not in returned_ids


def test_detect_failed_payments_idempotent(session):
    """Verify that calling detect_failed_payments twice returns the same count.

    Seeds 2 failed payments and calls the detector twice without any
    mutations between calls; asserts both calls return the same number of
    records.
    """
    session.add_all([
        _make_payment(status="failed", failure_code="gateway_timeout"),
        _make_payment(status="failed", failure_code="bank_downtime"),
    ])
    session.flush()

    first_run = detect_failed_payments(session)
    second_run = detect_failed_payments(session)

    assert len(first_run) == len(second_run) == 2


def test_detect_failed_payments_excludes_already_processed(session):
    """Verify that a failed payment with an existing recovery_action is excluded.

    Seeds 1 failed payment AND a recovery_action that references it with
    event_type='payment_failure'; asserts that detect_failed_payments
    returns 0 records (payment is already processed).
    """
    payment = _make_payment(status="failed", failure_code="card_declined")
    session.add(payment)
    session.flush()

    action = _make_recovery_action(payment.id, "payment_failure")
    session.add(action)
    session.flush()

    result = detect_failed_payments(session)

    assert len(result) == 0


def test_severity_mapping(session):
    """Verify the static failure-code → severity mapping.

    Asserts that:
      bank_downtime     → "high"
      gateway_timeout   → "high"
      card_declined     → "medium"
      insufficient_funds → "low"
    """
    assert _derive_severity("bank_downtime") == "high"
    assert _derive_severity("gateway_timeout") == "high"
    assert _derive_severity("card_declined") == "medium"
    assert _derive_severity("insufficient_funds") == "low"


def test_failure_summary_counts(session):
    """Verify that get_failure_summary returns correct per-code counts.

    Seeds 2 card_declined and 1 insufficient_funds failed payments; asserts
    that the by_failure_code breakdown has count=2 for card_declined and
    count=1 for insufficient_funds.
    """
    session.add_all([
        _make_payment(failure_code="card_declined",      amount=500.0),
        _make_payment(failure_code="card_declined",      amount=700.0),
        _make_payment(failure_code="insufficient_funds", amount=300.0),
    ])
    session.flush()

    summary = get_failure_summary(session)

    assert summary["total_failed"] == 3
    assert summary["by_failure_code"]["card_declined"]["count"] == 2
    assert summary["by_failure_code"]["insufficient_funds"]["count"] == 1


# ===========================================================================
# Checkout abandonment detector tests
# ===========================================================================


def test_detect_abandoned_checkouts_returns_only_abandoned(session):
    """Verify that detect_abandoned_checkouts returns only status='abandoned' sessions.

    Seeds 1 completed and 2 abandoned sessions; asserts that exactly 2
    records are returned (the completed session is excluded).
    """
    s_completed = _make_checkout(status="completed", cart_value=4000.0)
    s_abandoned1 = _make_checkout(status="abandoned", cart_value=2500.0)
    s_abandoned2 = _make_checkout(status="abandoned", cart_value=6000.0)
    session.add_all([s_completed, s_abandoned1, s_abandoned2])
    session.flush()

    result = detect_abandoned_checkouts(session)

    assert len(result) == 2
    returned_ids = {r["session_id"] for r in result}
    assert s_abandoned1.id in returned_ids
    assert s_abandoned2.id in returned_ids
    assert s_completed.id not in returned_ids


def test_checkout_idempotent(session):
    """Verify that detect_abandoned_checkouts is idempotent.

    Seeds 2 abandoned sessions and calls the detector twice without any
    mutations between calls; asserts both calls return the same count.
    """
    session.add_all([
        _make_checkout(status="abandoned", cart_value=1000.0),
        _make_checkout(status="abandoned", cart_value=2000.0),
    ])
    session.flush()

    first_run = detect_abandoned_checkouts(session)
    second_run = detect_abandoned_checkouts(session)

    assert len(first_run) == len(second_run) == 2


def test_checkout_excludes_processed(session):
    """Verify that an abandoned session with an existing recovery_action is excluded.

    Seeds 1 abandoned session AND a recovery_action referencing it with
    event_type='checkout_abandonment'; asserts that detect_abandoned_checkouts
    returns 0 records (session is already processed).
    """
    checkout = _make_checkout(status="abandoned", cart_value=3000.0)
    session.add(checkout)
    session.flush()

    action = _make_recovery_action(checkout.id, "checkout_abandonment")
    session.add(action)
    session.flush()

    result = detect_abandoned_checkouts(session)

    assert len(result) == 0


def test_recovery_priority_mapping(session):
    """Verify the static cart_value → recovery priority mapping.

    Asserts that:
      cart_value >= 5000 → "high"
      cart_value >= 2000 → "medium"
      cart_value <  2000 → "low"
    """
    assert _derive_priority(Decimal("5000")) == "high"
    assert _derive_priority(Decimal("7500")) == "high"
    assert _derive_priority(Decimal("2000")) == "medium"
    assert _derive_priority(Decimal("3500")) == "medium"
    assert _derive_priority(Decimal("1999")) == "low"
    assert _derive_priority(Decimal("0")) == "low"


def test_abandonment_summary(session):
    """Verify that get_abandonment_summary returns correct by_priority breakdown.

    Seeds:
      2 high-priority  sessions (cart_value >= 5000)
      1 medium-priority session (cart_value in [2000, 5000))
      1 low-priority   session (cart_value < 2000)
    Asserts counts in the by_priority dict match expectations.
    """
    session.add_all([
        _make_checkout(status="abandoned", cart_value=6000.0),
        _make_checkout(status="abandoned", cart_value=5000.0),
        _make_checkout(status="abandoned", cart_value=3000.0),
        _make_checkout(status="abandoned", cart_value=500.0),
    ])
    session.flush()

    summary = get_abandonment_summary(session)

    assert summary["total_abandoned"] == 4
    assert summary["by_priority"]["high"]["count"] == 2
    assert summary["by_priority"]["medium"]["count"] == 1
    assert summary["by_priority"]["low"]["count"] == 1
