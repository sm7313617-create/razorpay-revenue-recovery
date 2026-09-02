"""
tests/test_agent.py
----------------------------------------------------------------
Unit tests for the deterministic logic in agent/nodes.py.

ChatGoogleGenerativeAI is mocked everywhere in this module — no real
Gemini API call is ever made during the test run.

Patching strategy:
  unittest.mock.patch("agent.nodes.ChatGoogleGenerativeAI", ...)
  targets the name as it is used *inside* agent.nodes, which is the
  correct pattern for patching imported names.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from agent.nodes import (
    AgentState,
    check_stopping_rules,
    decide_intervention,
    prepare_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_state(**overrides) -> AgentState:
    """Return a fully-populated AgentState with safe default values.

    All fields required by TypedDict are present; callers may override
    individual fields via keyword arguments.
    """
    state: AgentState = {
        "event_type": "payment_failure",
        "reference_id": str(uuid.uuid4()),
        "event_data": {
            "failure_code": "card_declined",
            "amount": 1000.0,
            "severity": "medium",
            "merchant_id": "m1",
        },
        "attempt_count": 0,
        "agent_decision": "",
        "action_params": {},
        "outcome": "",
        "escalated": False,
        "error_detail": None,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


# ===========================================================================
# check_stopping_rules tests
# ===========================================================================


def test_check_stopping_rules_attempt_count():
    """Verify Rule 1: attempt_count >= 3 causes escalation.

    Constructs a state with attempt_count=3 and asserts that
    check_stopping_rules sets agent_decision='escalate' and
    escalated=True.
    """
    state = _base_state(attempt_count=3)

    result = check_stopping_rules(state)

    assert result["agent_decision"] == "escalate"
    assert result["escalated"] is True


def test_check_stopping_rules_bank_downtime():
    """Verify Rule 2: payment_failure + bank_downtime triggers notify_then_escalate.

    Constructs a payment_failure state with failure_code='bank_downtime'
    and attempt_count=0; asserts that check_stopping_rules sets
    agent_decision='notify_then_escalate' and escalated=True.
    """
    state = _base_state(
        event_type="payment_failure",
        attempt_count=0,
        event_data={
            "failure_code": "bank_downtime",
            "amount": 2000.0,
            "severity": "high",
            "merchant_id": "m1",
        },
    )

    result = check_stopping_rules(state)

    assert result["agent_decision"] == "notify_then_escalate"
    assert result["escalated"] is True


def test_check_stopping_rules_stale_checkout():
    """Verify Rule 3: checkout_abandonment older than 120 minutes triggers notify_only.

    Constructs a checkout_abandonment state with
    minutes_since_abandonment=150 and asserts that check_stopping_rules
    sets agent_decision='notify_only' (escalated remains unchanged).
    """
    state = _base_state(
        event_type="checkout_abandonment",
        attempt_count=0,
        event_data={
            "cart_value": 3000.0,
            "minutes_since_abandonment": 150,
            "recovery_priority": "medium",
            "merchant_id": "m1",
        },
    )

    result = check_stopping_rules(state)

    assert result["agent_decision"] == "notify_only"


def test_check_stopping_rules_no_trigger():
    """Verify that a normal event passes through check_stopping_rules unchanged.

    Constructs a state with attempt_count=0, a non-bank_downtime
    failure_code, and asserts that agent_decision remains an empty string
    (the function returns state unchanged so decide_intervention is called).
    """
    state = _base_state(
        attempt_count=0,
        event_data={
            "failure_code": "card_declined",
            "amount": 500.0,
            "severity": "medium",
            "merchant_id": "m1",
        },
    )

    result = check_stopping_rules(state)

    # No stopping rule matched — state is returned unchanged
    assert result["agent_decision"] in ("", None)
    assert result["escalated"] is False


# ===========================================================================
# decide_intervention tests
# ===========================================================================


def test_decide_intervention_uses_gemini():
    """Verify that decide_intervention stores the Gemini response as agent_decision.

    Mocks ChatGoogleGenerativeAI so that llm.invoke() returns a mock
    response with content='retry'; asserts that agent_decision is set to
    'retry' and error_detail remains None.
    """
    state = _base_state(agent_decision="")

    mock_response = MagicMock()
    mock_response.content = "retry"

    mock_llm_instance = MagicMock()
    mock_llm_instance.invoke.return_value = mock_response

    with patch("agent.nodes.ChatGoogleGenerativeAI", return_value=mock_llm_instance):
        result = decide_intervention(state)

    assert result["agent_decision"] == "retry"
    assert result["error_detail"] is None


def test_decide_intervention_gemini_error_fallback():
    """Verify that decide_intervention falls back to 'escalate' on any LLM exception.

    Mocks ChatGoogleGenerativeAI so that llm.invoke() raises a generic
    Exception; asserts that agent_decision is set to 'escalate' and
    error_detail is not None (contains the exception message).
    """
    state = _base_state(agent_decision="")

    mock_llm_instance = MagicMock()
    mock_llm_instance.invoke.side_effect = Exception("API quota exceeded")

    with patch("agent.nodes.ChatGoogleGenerativeAI", return_value=mock_llm_instance):
        result = decide_intervention(state)

    assert result["agent_decision"] == "escalate"
    assert result["error_detail"] is not None
    assert "API quota exceeded" in result["error_detail"]


# ===========================================================================
# prepare_action tests
# ===========================================================================


def test_prepare_action_retry_params():
    """Verify that prepare_action produces correct params for a 'retry' decision.

    Constructs a state with agent_decision='retry'; asserts that the
    returned action_params contains a 'backoff_seconds' list and a
    'max_retries' key.
    """
    state = _base_state(agent_decision="retry")

    result = prepare_action(state)

    params = result["action_params"]
    assert "backoff_seconds" in params
    assert isinstance(params["backoff_seconds"], list)
    assert len(params["backoff_seconds"]) > 0
    assert "max_retries" in params


def test_prepare_action_escalate_sets_flag():
    """Verify that prepare_action sets escalated=True for an 'escalate' decision.

    Constructs a state with agent_decision='escalate' and escalated=False;
    asserts that the returned state contains escalated=True.
    """
    state = _base_state(agent_decision="escalate", escalated=False)

    result = prepare_action(state)

    assert result["escalated"] is True


def test_prepare_action_discount_params():
    """Verify the discount percentage rule: >=5000 → 10%, <5000 → 15%.

    Tests two states:
      1. cart_value=6000 → expects percent=10
      2. cart_value=4000 → expects percent=15
    """
    # High-value cart — 10% discount
    state_high = _base_state(
        agent_decision="discount",
        event_data={"cart_value": 6000.0, "merchant_id": "m1"},
    )
    result_high = prepare_action(state_high)
    assert result_high["action_params"]["percent"] == 10

    # Lower-value cart — 15% discount
    state_low = _base_state(
        agent_decision="discount",
        event_data={"cart_value": 4000.0, "merchant_id": "m1"},
    )
    result_low = prepare_action(state_low)
    assert result_low["action_params"]["percent"] == 15
