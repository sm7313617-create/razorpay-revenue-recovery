"""
audit/logger.py
----------------------------------------------------------------
Standalone audit query utility for the Razorpay AI Revenue Recovery
agent.  This module is completely independent of agent/nodes.py
(which contains the log_audit_entry *node* that writes rows).
This module only *reads* and pretty-prints those rows.

Public API
----------
get_audit_trail(session, reference_id)  -> list[dict]
    All audit_log rows for one reference_id, ascending by timestamp.

get_full_audit_log(session, limit)      -> list[dict]
    Most recent N entries across all events, descending by timestamp.

print_audit_trail(reference_id)         -> None
    Standalone pretty-printer that creates its own DB session and
    renders the trail as a formatted pandas DataFrame.

get_escalation_report(session)          -> dict
    Aggregate escalation statistics.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import AuditLog

# ---------------------------------------------------------------------------
# Load environment once at module level
# ---------------------------------------------------------------------------

load_dotenv()


# ---------------------------------------------------------------------------
# Internal serialiser
# ---------------------------------------------------------------------------


def _row_to_dict(row: AuditLog) -> dict[str, Any]:
    """Serialise an AuditLog ORM instance to a plain dict.

    UUID fields are converted to strings; datetime fields are converted
    to ISO-8601 strings with timezone information preserved.

    Args:
        row: An AuditLog ORM instance.

    Returns:
        A JSON-safe dict representing the audit row.
    """
    return {
        "id":            str(row.id),
        "timestamp":     row.timestamp.isoformat() if row.timestamp else None,
        "event_type":    row.event_type,
        "reference_id":  str(row.reference_id) if row.reference_id else None,
        "agent_state":   row.agent_state,
        "decision_made": row.decision_made,
        "action_taken":  row.action_taken,
        "outcome":       row.outcome,
        "escalated":     row.escalated,
        "error_detail":  row.error_detail,
    }


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


def get_audit_trail(session: Session, reference_id: str) -> list[dict[str, Any]]:
    """Return all audit_log rows for one reference_id, ordered chronologically.

    Args:
        session:      An active SQLAlchemy Session.
        reference_id: String UUID of the payment or checkout session.

    Returns:
        List of audit row dicts ordered by timestamp ascending.
        Returns an empty list if the reference_id is invalid or has no rows.
    """
    try:
        ref_uuid = uuid.UUID(reference_id)
    except (ValueError, AttributeError):
        return []

    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.reference_id == ref_uuid)
        .order_by(AuditLog.timestamp.asc())
    ).all()

    return [_row_to_dict(r) for r in rows]


def get_full_audit_log(
    session: Session, limit: int = 100
) -> list[dict[str, Any]]:
    """Return the most recent *limit* audit entries across all events.

    Args:
        session: An active SQLAlchemy Session.
        limit:   Maximum number of rows to return (default 100).

    Returns:
        List of audit row dicts ordered by timestamp descending.
    """
    rows = session.scalars(
        select(AuditLog)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
    ).all()

    return [_row_to_dict(r) for r in rows]


def print_audit_trail(reference_id: str) -> None:
    """Pretty-print the full decision trail for one reference_id.

    Creates its own database engine and session (uses DB_URL from the
    environment).  Renders the trail as a formatted pandas DataFrame
    with the following columns:
        timestamp, event_type, agent_state, decision_made,
        action_taken, outcome, escalated

    Args:
        reference_id: String UUID of the payment or checkout session.
    """
    db_url: str = os.environ["DB_URL"]
    engine = create_engine(db_url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    try:
        with SessionLocal() as session:
            rows = get_audit_trail(session, reference_id)
    finally:
        engine.dispose()

    print(f"\n{'=' * 70}")
    print(f"  AUDIT TRAIL — {reference_id}")
    print(f"{'=' * 70}\n")

    if not rows:
        print("  No audit entries found for this reference_id.\n")
        return

    display_cols = [
        "timestamp",
        "event_type",
        "agent_state",
        "decision_made",
        "action_taken",
        "outcome",
        "escalated",
    ]

    df = pd.DataFrame(rows)[display_cols]

    # Truncate long strings for readability
    df["action_taken"] = df["action_taken"].str[:40]
    df["decision_made"] = df["decision_made"].str[:30]

    # Friendly timestamp display
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    pd.set_option("display.max_colwidth", 40)
    pd.set_option("display.width", 140)
    print(df.to_string(index=False))
    print()


def get_escalation_report(session: Session) -> dict[str, Any]:
    """Return aggregate escalation statistics from the audit_log table.

    Computes:
    - total_escalated      : count of rows where escalated = True
    - escalation_by_event_type : {event_type: count} breakdown
    - escalated_references : list of {reference_id, error_detail} dicts

    Args:
        session: An active SQLAlchemy Session.

    Returns:
        A dict with keys total_escalated, escalation_by_event_type,
        and escalated_references.
    """
    # Total count
    total_escalated: int = session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.escalated.is_(True))
    ) or 0

    # Breakdown by event_type
    type_counts = session.execute(
        select(AuditLog.event_type, func.count(AuditLog.id))
        .where(AuditLog.escalated.is_(True))
        .group_by(AuditLog.event_type)
    ).all()

    escalation_by_event_type: dict[str, int] = {
        event_type: count for event_type, count in type_counts
    }

    # Individual escalated references with error detail
    escalated_rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.escalated.is_(True))
        .order_by(AuditLog.timestamp.desc())
    ).all()

    escalated_references: list[dict[str, Any]] = [
        {
            "reference_id": str(r.reference_id) if r.reference_id else None,
            "error_detail": r.error_detail,
        }
        for r in escalated_rows
    ]

    return {
        "total_escalated":          total_escalated,
        "escalation_by_event_type": escalation_by_event_type,
        "escalated_references":     escalated_references,
    }
