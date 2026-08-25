"""
db/models.py
----------------------------------------------------------------
SQLAlchemy 2.0 ORM models for the Razorpay AI Revenue Recovery agent.

Tables
------
* payments            - payment attempt lifecycle
* checkout_sessions   - cart / checkout lifecycle
* recovery_actions    - bounded recovery workflow records
* audit_log           - immutable agent decision log
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, Enum, JSON, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ----------------------------------------------------------------
# Declarative base (SQLAlchemy 2.0 style — resolves all Pylance warnings)
# ----------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ----------------------------------------------------------------
# Enum types (identical names/values as before — no DDL change)
# ----------------------------------------------------------------

PaymentStatus = Enum(
    "initiated", "failed", "success",
    name="payment_status",
)

FailureCode = Enum(
    "insufficient_funds", "card_declined", "gateway_timeout", "bank_downtime",
    name="failure_code",
)

CheckoutStatus = Enum(
    "active", "abandoned", "completed",
    name="checkout_status",
)

RecoveryEventType = Enum(
    "payment_failure", "checkout_abandonment",
    name="recovery_event_type",
)

RecoveryActionTaken = Enum(
    "retry", "notify", "discount", "escalate",
    name="recovery_action_taken",
)

RecoveryStatus = Enum(
    "pending", "success", "failed", "escalated",
    name="recovery_status",
)


# ----------------------------------------------------------------
# Models
# ----------------------------------------------------------------

class Payment(Base):
    """Records every payment attempt made through the platform."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(PaymentStatus, nullable=False, default="initiated")
    failure_code: Mapped[Optional[str]] = mapped_column(FailureCode, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id} merchant={self.merchant_id} "
            f"amount={self.amount} {self.currency} status={self.status}>"
        )


class CheckoutSession(Base):
    """Tracks cart and checkout sessions to detect abandonment."""

    __tablename__ = "checkout_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cart_value: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=2), nullable=False)
    status: Mapped[str] = mapped_column(CheckoutStatus, nullable=False, default="active")
    abandoned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<CheckoutSession id={self.id} merchant={self.merchant_id} "
            f"cart_value={self.cart_value} status={self.status}>"
        )


class RecoveryAction(Base):
    """Records every bounded recovery action taken by the agent."""

    __tablename__ = "recovery_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(RecoveryEventType, nullable=False)
    # reference_id is a logical FK — it points to either payments.id or
    # checkout_sessions.id depending on event_type.  A DB-level FK to a
    # single table would break referential integrity when the other table
    # is referenced, so we store it as a plain UUID and enforce integrity
    # at the application layer.
    reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    action_taken: Mapped[str] = mapped_column(RecoveryActionTaken, nullable=False)
    action_params: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(RecoveryStatus, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<RecoveryAction id={self.id} event_type={self.event_type} "
            f"action={self.action_taken} status={self.status}>"
        )


class AuditLog(Base):
    """Immutable record of every agent decision - never mutated after insert."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    agent_state: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_made: Mapped[str] = mapped_column(Text, nullable=False)
    action_taken: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_context: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} event_type={self.event_type} "
            f"outcome={self.outcome} escalated={self.escalated}>"
        )
