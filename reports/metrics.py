"""
reports/metrics.py
----------------------------------------------------------------
Metrics and reporting engine for the Razorpay AI Revenue Recovery agent.

Computes recovery rates, financial impact, breakdown by failure code and
action taken, agent decision audit trails, exception reports, and baseline
comparisons for judging and dashboard presentation.

Public API
----------
generate_full_report(session) -> dict
    Master reporting function returning all metric sections.

get_summary_metrics(session) -> dict
    Section 1: Event throughput summary.

get_recovery_outcomes(session) -> dict
    Section 2: Event-level recovery outcome distribution and recovery rate.

get_financial_impact(session) -> dict
    Section 3: Revenue saved, amount at risk, and engaged cart value.

get_by_failure_code(session) -> list[dict]
    Section 4: Performance breakdown by payment failure code.

get_by_action_taken(session) -> list[dict]
    Section 5: Performance breakdown by intervention action type.

get_agent_decisions(session) -> dict
    Section 6: Rule triggers, LLM classification, and escalation rates.

get_exceptions(session) -> list[dict]
    Section 7: Escalation and error audit records.

generate_baseline_comparison(session) -> dict
    Comparison against a deterministic single-retry / notify-all baseline.

print_report(report) -> None
    Pretty-prints formatted report with pandas tabular displays.

export_report_json(report, path) -> None
    Exports full report dictionary to a JSON file.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import case, create_engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

# Allow running directly as a script from project root or reports/ directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import AuditLog, CheckoutSession, Payment, RecoveryAction
from interventions.retry import _simulate_retry


# ---------------------------------------------------------------------------
# Section 1: Summary Metrics
# ---------------------------------------------------------------------------


def get_summary_metrics(session: Session) -> dict[str, Any]:
    """Calculate event throughput summary metrics.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        Dict containing total_events_processed,
        total_payment_failures_processed,
        total_checkout_abandonments_processed, and run_timestamp.
    """
    total_events = (
        session.scalar(
            select(func.count(func.distinct(RecoveryAction.reference_id)))
        )
        or 0
    )

    total_payment_failures = (
        session.scalar(
            select(func.count(func.distinct(RecoveryAction.reference_id))).where(
                RecoveryAction.event_type == "payment_failure"
            )
        )
        or 0
    )

    total_checkout_abandonments = (
        session.scalar(
            select(func.count(func.distinct(RecoveryAction.reference_id))).where(
                RecoveryAction.event_type == "checkout_abandonment"
            )
        )
        or 0
    )

    return {
        "total_events_processed": int(total_events),
        "total_payment_failures_processed": int(total_payment_failures),
        "total_checkout_abandonments_processed": int(
            total_checkout_abandonments
        ),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Section 2: Recovery Outcomes (Event-Level)
# ---------------------------------------------------------------------------


def get_recovery_outcomes(
    session: Session, total_events: int | None = None
) -> dict[str, Any]:
    """Calculate event-level recovery outcome counts and recovery rate percentage.

    Standardized on an event-level (distinct reference_id) definition:
      - recovered: event has at least one recovery_action with status='success'.
      - escalated: event was not recovered, but has an action_taken='escalate'
        or status='escalated' action handed off to human review.
      - failed: event has a failed action with no successful recovery/escalation.
      - pending: event is currently in-flight / pending.

    Args:
        session: Active SQLAlchemy Session.
        total_events: Optional denominator for recovery rate. If omitted,
            calculated from distinct reference IDs in recovery_actions.

    Returns:
        Dict containing recovered_count, failed_count, escalated_count,
        pending_count, and recovery_rate_pct.
    """
    all_actions = session.scalars(select(RecoveryAction)).all()

    # Group actions by distinct reference_id
    by_ref: dict[uuid.UUID, list[RecoveryAction]] = {}
    for action in all_actions:
        by_ref.setdefault(action.reference_id, []).append(action)

    if total_events is None:
        total_events = len(by_ref)

    recovered = 0
    escalated = 0
    failed = 0
    pending = 0

    for _ref_id, actions in by_ref.items():
        statuses = {a.status for a in actions}
        action_types = {a.action_taken for a in actions}

        if "success" in statuses:
            recovered += 1
        elif "escalate" in action_types or "escalated" in statuses:
            escalated += 1
        elif "failed" in statuses:
            failed += 1
        else:
            pending += 1

    recovery_rate_pct = (
        round((recovered / total_events) * 100, 1) if total_events > 0 else 0.0
    )

    return {
        "recovered_count": int(recovered),
        "failed_count": int(failed),
        "escalated_count": int(escalated),
        "pending_count": int(pending),
        "recovery_rate_pct": recovery_rate_pct,
    }


# ---------------------------------------------------------------------------
# Section 3: Financial Impact
# ---------------------------------------------------------------------------


def get_financial_impact(session: Session) -> dict[str, Any]:
    """Compute financial metrics including revenue saved and cart values at risk.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        Dict containing total_amount_at_risk, amount_recovered,
        amount_still_at_risk, recovery_value_pct, cart_value_at_risk,
        and cart_value_engaged.
    """
    # Total failed payment amount initially at risk
    raw_total_at_risk = session.scalar(
        select(func.sum(Payment.amount)).where(Payment.failure_code.is_not(None))
    )
    total_amount_at_risk = (
        float(round(raw_total_at_risk, 2))
        if raw_total_at_risk is not None
        else 0.0
    )

    # Amount recovered: initially failed payments that are now status='success'
    raw_recovered = session.scalar(
        select(func.sum(Payment.amount)).where(
            Payment.failure_code.is_not(None),
            Payment.status == "success",
        )
    )
    amount_recovered = (
        float(round(raw_recovered, 2)) if raw_recovered is not None else 0.0
    )

    amount_still_at_risk = round(total_amount_at_risk - amount_recovered, 2)

    recovery_value_pct = (
        round((amount_recovered / total_amount_at_risk) * 100, 1)
        if total_amount_at_risk > 0
        else 0.0
    )

    # Cart value at risk for abandoned checkout sessions
    raw_cart_at_risk = session.scalar(
        select(func.sum(CheckoutSession.cart_value)).where(
            CheckoutSession.status == "abandoned"
        )
    )
    cart_value_at_risk = (
        float(round(raw_cart_at_risk, 2))
        if raw_cart_at_risk is not None
        else 0.0
    )

    # Cart value for abandoned sessions that received a recovery action
    checkout_subquery = select(RecoveryAction.reference_id).where(
        RecoveryAction.event_type == "checkout_abandonment"
    )
    raw_cart_engaged = session.scalar(
        select(func.sum(CheckoutSession.cart_value)).where(
            CheckoutSession.id.in_(checkout_subquery)
        )
    )
    cart_value_engaged = (
        float(round(raw_cart_engaged, 2))
        if raw_cart_engaged is not None
        else 0.0
    )

    return {
        "total_amount_at_risk": total_amount_at_risk,
        "amount_recovered": amount_recovered,
        "amount_still_at_risk": amount_still_at_risk,
        "recovery_value_pct": recovery_value_pct,
        "cart_value_at_risk": cart_value_at_risk,
        "cart_value_engaged": cart_value_engaged,
    }


# ---------------------------------------------------------------------------
# Section 4: Breakdown by Failure Code
# ---------------------------------------------------------------------------


def get_by_failure_code(session: Session) -> list[dict[str, Any]]:
    """Compute recovery performance breakdown for each payment failure code.

    Evaluates processed, recovered, and escalated event counts per failure code
    using the standardized event-level definition.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        List of dicts with failure_code, processed, recovered,
        escalated, and recovery_rate_pct.
    """
    all_codes = [
        "insufficient_funds",
        "card_declined",
        "gateway_timeout",
        "bank_downtime",
    ]

    payments = session.scalars(
        select(Payment).where(Payment.failure_code.is_not(None))
    ).all()
    payment_code_map = {p.id: p.failure_code for p in payments}

    pay_actions = session.scalars(
        select(RecoveryAction).where(
            RecoveryAction.event_type == "payment_failure"
        )
    ).all()

    by_ref: dict[uuid.UUID, list[RecoveryAction]] = {}
    for action in pay_actions:
        by_ref.setdefault(action.reference_id, []).append(action)

    breakdown: list[dict[str, Any]] = []
    for code in all_codes:
        code_refs = [
            ref for ref in by_ref if payment_code_map.get(ref) == code
        ]
        proc = len(code_refs)
        rec = sum(
            1
            for ref in code_refs
            if any(a.status == "success" for a in by_ref[ref])
        )
        esc = sum(
            1
            for ref in code_refs
            if any(
                a.action_taken == "escalate" or a.status == "escalated"
                for a in by_ref[ref]
            )
        )
        rate = round((rec / proc) * 100, 1) if proc > 0 else 0.0

        breakdown.append(
            {
                "failure_code": code,
                "processed": proc,
                "recovered": rec,
                "escalated": esc,
                "recovery_rate_pct": rate,
            }
        )

    return breakdown


# ---------------------------------------------------------------------------
# Section 5: Breakdown by Action Taken
# ---------------------------------------------------------------------------


def get_by_action_taken(session: Session) -> list[dict[str, Any]]:
    """Compute action usage, success, and failure counts for each action type.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        List of dicts with action_taken, count_used, success_count,
        and failure_count.
    """
    all_actions = ["retry", "notify", "discount", "escalate"]

    stmt = select(
        RecoveryAction.action_taken,
        func.count(RecoveryAction.id).label("count_used"),
        func.count(
            case(
                (RecoveryAction.status == "success", RecoveryAction.id),
                else_=None,
            )
        ).label("success_count"),
        func.count(
            case(
                (RecoveryAction.status == "failed", RecoveryAction.id),
                else_=None,
            )
        ).label("failure_count"),
    ).group_by(RecoveryAction.action_taken)

    results = session.execute(stmt).all()
    stats_map = {
        row[0]: {
            "count_used": int(row[1] or 0),
            "success_count": int(row[2] or 0),
            "failure_count": int(row[3] or 0),
        }
        for row in results
    }

    breakdown: list[dict[str, Any]] = []
    for action in all_actions:
        data = stats_map.get(
            action, {"count_used": 0, "success_count": 0, "failure_count": 0}
        )
        breakdown.append(
            {
                "action_taken": action,
                "count_used": data["count_used"],
                "success_count": data["success_count"],
                "failure_count": data["failure_count"],
            }
        )

    return breakdown


# ---------------------------------------------------------------------------
# Section 6: Agent Decisions
# ---------------------------------------------------------------------------


def get_agent_decisions(session: Session) -> dict[str, Any]:
    """Analyze agent decisions from audit_log table.

    Differentiates stopping rule triggers from LLM classifications and
    tracks escalation and error rates.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        Dict containing stopping_rule_triggered_count, gemini_decided_count,
        gemini_escalation_rate_pct, and error_count.
    """
    stopping_rules = ["notify_then_escalate", "notify_only"]
    gemini_decisions = ["retry", "notify", "discount", "escalate"]

    stopping_count = (
        session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.decision_made.in_(stopping_rules)
            )
        )
        or 0
    )

    gemini_count = (
        session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.decision_made.in_(gemini_decisions)
            )
        )
        or 0
    )

    gemini_escalate_count = (
        session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.decision_made == "escalate"
            )
        )
        or 0
    )

    gemini_esc_rate = (
        round((gemini_escalate_count / gemini_count) * 100, 1)
        if gemini_count > 0
        else 0.0
    )

    error_count = (
        session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.error_detail.is_not(None)
            )
        )
        or 0
    )

    return {
        "stopping_rule_triggered_count": int(stopping_count),
        "gemini_decided_count": int(gemini_count),
        "gemini_escalation_rate_pct": gemini_esc_rate,
        "error_count": int(error_count),
    }


# ---------------------------------------------------------------------------
# Section 7: Exceptions List
# ---------------------------------------------------------------------------


def get_exceptions(session: Session) -> list[dict[str, Any]]:
    """Retrieve all escalated or error events from the audit log.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        List of dicts representing unrecoverable or escalated events.
    """
    stmt = (
        select(AuditLog)
        .where(
            or_(
                AuditLog.escalated.is_(True),
                AuditLog.error_detail.is_not(None),
                AuditLog.outcome.in_(["escalated", "error"]),
            )
        )
        .order_by(AuditLog.timestamp.desc())
    )

    rows = session.scalars(stmt).all()
    exceptions: list[dict[str, Any]] = [
        {
            "reference_id": str(r.reference_id) if r.reference_id else None,
            "event_type": r.event_type,
            "decision_made": r.decision_made,
            "error_detail": r.error_detail,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        }
        for r in rows
    ]

    return exceptions


# ---------------------------------------------------------------------------
# Baseline Comparison
# ---------------------------------------------------------------------------


def generate_baseline_comparison(session: Session) -> dict[str, Any]:
    """Compute comparison against a simple deterministic baseline.

    The baseline policy retries all payment failures once and notifies
    all abandoned checkouts once. Evaluated across all events processed
    by the recovery engine using the standardized event-level definition.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        Dict containing baseline_recovery_count, baseline_recovery_rate_pct,
        agent_recovery_count, agent_recovery_rate_pct, and improvement_pct.
    """
    rec_actions = session.scalars(select(RecoveryAction)).all()
    processed_refs: dict[uuid.UUID, dict[str, Any]] = {}

    for ra in rec_actions:
        if ra.reference_id not in processed_refs:
            processed_refs[ra.reference_id] = {
                "event_type": ra.event_type,
                "status": ra.status,
            }
        elif ra.status == "success":
            processed_refs[ra.reference_id]["status"] = "success"

    total_events = len(processed_refs)
    agent_recovery_count = sum(
        1 for r in processed_refs.values() if r["status"] == "success"
    )
    agent_recovery_rate_pct = (
        round((agent_recovery_count / total_events) * 100, 1)
        if total_events > 0
        else 0.0
    )

    # Deterministic simulation of baseline
    baseline_recovery_count = 0
    for ref_id, info in processed_refs.items():
        if info["event_type"] == "payment_failure":
            p = session.scalar(select(Payment).where(Payment.id == ref_id))
            if p and _simulate_retry(p.id, p.failure_code):
                baseline_recovery_count += 1
        elif info["event_type"] == "checkout_abandonment":
            # Simple baseline sends 1 notification to all abandoned checkouts
            baseline_recovery_count += 1

    baseline_recovery_rate_pct = (
        round((baseline_recovery_count / total_events) * 100, 1)
        if total_events > 0
        else 0.0
    )

    improvement_pct = round(
        agent_recovery_rate_pct - baseline_recovery_rate_pct, 1
    )

    return {
        "baseline_recovery_count": baseline_recovery_count,
        "baseline_recovery_rate_pct": baseline_recovery_rate_pct,
        "agent_recovery_count": agent_recovery_count,
        "agent_recovery_rate_pct": agent_recovery_rate_pct,
        "improvement_pct": improvement_pct,
    }


# ---------------------------------------------------------------------------
# Master Report Function
# ---------------------------------------------------------------------------


def generate_full_report(session: Session) -> dict[str, Any]:
    """Generate the complete nested metrics dictionary across all sections.

    Args:
        session: Active SQLAlchemy Session.

    Returns:
        Nested dict with summary, recovery_outcomes, financial_impact,
        by_failure_code, by_action_taken, agent_decisions, exceptions,
        and baseline_comparison.
    """
    summary = get_summary_metrics(session)
    total_events = summary["total_events_processed"]

    outcomes = get_recovery_outcomes(session, total_events=total_events)
    financial = get_financial_impact(session)
    by_failure = get_by_failure_code(session)
    by_action = get_by_action_taken(session)
    decisions = get_agent_decisions(session)
    exceptions = get_exceptions(session)
    baseline = generate_baseline_comparison(session)

    return {
        "summary": summary,
        "recovery_outcomes": outcomes,
        "financial_impact": financial,
        "by_failure_code": by_failure,
        "by_action_taken": by_action,
        "agent_decisions": decisions,
        "exceptions": exceptions,
        "baseline_comparison": baseline,
    }


# ---------------------------------------------------------------------------
# Pretty Print & Export Utilities
# ---------------------------------------------------------------------------


def _json_serial_default(obj: Any) -> Any:
    """JSON serialization fallback for Decimal, UUID, and datetime objects."""
    if isinstance(obj, (Decimal, float)):
        return float(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (uuid.UUID,)):
        return str(obj)
    return str(obj)


def export_report_json(
    report: dict[str, Any], path: str = "reports/recovery_report.json"
) -> None:
    """Export the full report dictionary to a formatted JSON file.

    Args:
        report: Report dictionary from generate_full_report.
        path: Filepath destination for the JSON export.
    """
    abs_path = os.path.abspath(path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_json_serial_default)

    print(f"\n[OK] Report successfully exported to {path}")


def print_report(report: dict[str, Any]) -> None:
    """Pretty-print the full report to the console using pandas DataFrames.

    Args:
        report: Report dictionary from generate_full_report.
    """
    sep_major = "=" * 75
    sep_minor = "-" * 75

    print("\n" + sep_major)
    print("       RAZORPAY AI REVENUE RECOVERY -- COMPREHENSIVE METRICS REPORT")
    print(sep_major)

    # 1. Summary
    sum_data = report.get("summary", {})
    print(f"\n[SECTION 1: SUMMARY]")
    print(f"  Run Timestamp                         : {sum_data.get('run_timestamp')}")
    print(f"  Total Events Processed                : {sum_data.get('total_events_processed'):,}")
    print(f"  Payment Failures Processed            : {sum_data.get('total_payment_failures_processed'):,}")
    print(f"  Checkout Abandonments Processed       : {sum_data.get('total_checkout_abandonments_processed'):,}")

    # 2. Recovery Outcomes
    outcomes = report.get("recovery_outcomes", {})
    print(f"\n[SECTION 2: RECOVERY OUTCOMES]")
    print(f"  Recovered (Success)                   : {outcomes.get('recovered_count'):,}")
    print(f"  Failed                                : {outcomes.get('failed_count'):,}")
    print(f"  Escalated                             : {outcomes.get('escalated_count'):,}")
    print(f"  Pending                               : {outcomes.get('pending_count'):,}")
    print(f"  Recovery Rate                         : {outcomes.get('recovery_rate_pct', 0.0):.1f}%")

    # 3. Financial Impact
    fin = report.get("financial_impact", {})
    print(f"\n[SECTION 3: FINANCIAL IMPACT]")
    print(f"  Total Amount at Risk (Failed Payments): INR {fin.get('total_amount_at_risk', 0.0):,.2f}")
    print(f"  Amount Recovered                      : INR {fin.get('amount_recovered', 0.0):,.2f}")
    print(f"  Amount Still at Risk                  : INR {fin.get('amount_still_at_risk', 0.0):,.2f}")
    print(f"  Recovery Value Rate                   : {fin.get('recovery_value_pct', 0.0):.1f}%")
    print(f"  Cart Value at Risk (Abandoned Sessions): INR {fin.get('cart_value_at_risk', 0.0):,.2f}")
    print(f"  Cart Value Engaged with Actions       : INR {fin.get('cart_value_engaged', 0.0):,.2f}")

    # 4. By Failure Code
    print(f"\n[SECTION 4: BY PAYMENT FAILURE CODE]")
    by_code = report.get("by_failure_code", [])
    if by_code:
        df_code = pd.DataFrame(by_code)
        df_code["recovery_rate_pct"] = df_code["recovery_rate_pct"].map(
            lambda x: f"{x:.1f}%"
        )
        print(df_code.to_string(index=False))
    else:
        print("  No payment failure records found.")

    # 5. By Action Taken
    print(f"\n[SECTION 5: BY ACTION TAKEN]")
    by_act = report.get("by_action_taken", [])
    if by_act:
        df_act = pd.DataFrame(by_act)
        print(df_act.to_string(index=False))
    else:
        print("  No action records found.")

    # 6. Agent Decisions
    dec = report.get("agent_decisions", {})
    print(f"\n[SECTION 6: AGENT DECISIONS & AUDIT TRAIL]")
    print(f"  Stopping Rules Triggered (Bypassed LLM): {dec.get('stopping_rule_triggered_count'):,}")
    print(f"  Gemini Classifications Made            : {dec.get('gemini_decided_count'):,}")
    print(f"  Gemini Escalation Rate                 : {dec.get('gemini_escalation_rate_pct', 0.0):.1f}%")
    print(f"  System Errors Logged                   : {dec.get('error_count'):,}")

    # 7. Exceptions List
    print(f"\n[SECTION 7: EXCEPTIONS LIST (UNRESOLVED / ESCALATED)]")
    exc_list = report.get("exceptions", [])
    if exc_list:
        df_exc = pd.DataFrame(exc_list)
        # Format timestamps and truncate error strings for clean presentation
        if "timestamp" in df_exc.columns:
            df_exc["timestamp"] = pd.to_datetime(
                df_exc["timestamp"], utc=True
            ).dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        if "error_detail" in df_exc.columns:
            df_exc["error_detail"] = (
                df_exc["error_detail"].fillna("").astype(str).str[:50]
            )
        if "reference_id" in df_exc.columns:
            df_exc["reference_id"] = (
                df_exc["reference_id"].astype(str).str[:18] + "..."
            )
        print(df_exc.to_string(index=False))
    else:
        print("  No exceptions or escalations logged.")

    # Baseline Comparison
    base = report.get("baseline_comparison", {})
    print(f"\n[BASELINE COMPARISON]")
    print(f"  Baseline Recovery Count               : {base.get('baseline_recovery_count'):,}")
    print(f"  Baseline Recovery Rate                : {base.get('baseline_recovery_rate_pct', 0.0):.1f}%")
    print(f"  Agent Recovery Count                  : {base.get('agent_recovery_count'):,}")
    print(f"  Agent Recovery Rate                   : {base.get('agent_recovery_rate_pct', 0.0):.1f}%")
    imp_sign = "+" if base.get("improvement_pct", 0.0) >= 0 else ""
    print(f"  Agent vs Baseline Improvement         : {imp_sign}{base.get('improvement_pct', 0.0):.1f}%")

    print("\n" + sep_major + "\n")


# ---------------------------------------------------------------------------
# CLI Execution Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_dotenv()
    db_url = os.environ.get("DB_URL")
    if not db_url:
        print("[ERROR] DB_URL is not set in environment.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    try:
        with SessionLocal() as session:
            report_data = generate_full_report(session)
            print_report(report_data)
            export_report_json(report_data)
    finally:
        engine.dispose()
