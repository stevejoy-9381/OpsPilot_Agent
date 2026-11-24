"""Offline unit tests for B2 No-Tools LLM Baseline (baseline/b2_llm_no_tools.py).

Verifies:
1. Valid structured output path with FakeLLMClient.
2. Schema failure path (records schema_failure, abstains diagnosis, escalates).
3. Timeout path (records timeout, abstains diagnosis, escalates).
4. Provider error path (records provider_error, abstains diagnosis, escalates).
5. Token budget guard enforcement (BudgetExceededError handled gracefully).
6. Zero leakage in prompt template (passes Feature 1.13 scan).
7. Pricing configuration and cost calculation.
8. Multi-trial execution (--trials N support).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from agent.llm.fake import FakeLLMClient
from agent.llm.interface import (
    LLMError,
    TokenBudgetGuard,
)
from baseline.alerts.models import AlertEvent
from baseline.b2_llm_no_tools import (
    B2LLMResponse,
    calculate_cost,
    load_pricing_config,
    load_prompt_template,
    run_b2,
    run_b2_trials,
)
from evals.graders.models import Diagnosis
from shared.actions import ActionType, RecommendedAction
from tests.test_leakage import FORBIDDEN_FAMILY_NAMES, FORBIDDEN_TERMS_PATTERNS


@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        alert_id="alt_orders_001",
        fired_at=datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc),
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=9.5,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
    )


def test_b2_valid_structured_output_path(sample_alert: AlertEvent) -> None:
    """Requirement: valid structured output path returns parsed diagnosis and action."""
    diag = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        culprit_deploy_id=None,
        evidence_ids=[],
        confidence=0.15,
    )
    act = RecommendedAction(
        action=ActionType.ESCALATE,
        service=None,
        rationale_ids=[],
        notes="Alert indicates orders pool pressure; escalation required.",
    )
    resp = B2LLMResponse(
        diagnosis=diag,
        action=act,
        confidence=0.15,
        uncertainty_note="No live logs or deployment records available to verify root cause.",
    )

    client = FakeLLMClient(responses=[resp], model_name="fake-gemini")
    res = run_b2(sample_alert, client=client, scenario_id="S1")

    assert res.baseline_name == "B2"
    assert res.scenario_id == "S1"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.action.action == ActionType.ESCALATE
    assert res.error_type is None
    assert res.tokens_in > 0
    assert res.tokens_out > 0
    assert res.cost_usd >= 0.0
    assert res.prompt_hash is not None
    assert len(res.prompt_hash) == 64
    assert res.uncertainty_note is not None


def test_b2_schema_failure_path(sample_alert: AlertEvent) -> None:
    """Requirement: schema failure is recorded as schema_failure, not wrong diagnosis."""
    # Dict missing required fields triggers schema_validation_failed in FakeLLMClient
    invalid_dict = {"invalid_field": 123}
    client = FakeLLMClient(responses=[invalid_dict], model_name="fake-gemini")

    res = run_b2(sample_alert, client=client, scenario_id="S2")

    assert res.error_type == "schema_failure"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.diagnosis.mechanism == "unknown"
    assert res.diagnosis.confidence == 0.0
    assert res.action.action == ActionType.ESCALATE


def test_b2_timeout_path(sample_alert: AlertEvent) -> None:
    """Requirement: timeout error is recorded as timeout, never counted as wrong diagnosis."""
    timeout_err = LLMError(error_type="timeout", message="Deadline exceeded after 30s")
    client = FakeLLMClient(responses=[{"error": timeout_err}], model_name="fake-gemini")

    res = run_b2(sample_alert, client=client, scenario_id="S3")

    assert res.error_type == "timeout"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.diagnosis.mechanism == "unknown"
    assert res.action.action == ActionType.ESCALATE


def test_b2_provider_error_path(sample_alert: AlertEvent) -> None:
    """Requirement: provider error is recorded as provider_error, not wrong diagnosis."""
    prov_err = LLMError(error_type="provider_error", message="503 Service Unavailable")
    client = FakeLLMClient(responses=[{"error": prov_err}], model_name="fake-gemini")

    res = run_b2(sample_alert, client=client, scenario_id="S4")

    assert res.error_type == "provider_error"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.diagnosis.mechanism == "unknown"
    assert res.action.action == ActionType.ESCALATE


def test_b2_budget_guard(sample_alert: AlertEvent) -> None:
    """Requirement: TokenBudgetGuard is enforced and breaches are handled safely."""
    guard = TokenBudgetGuard(max_tokens=50)

    # Fake client that consumes 80 tokens (exceeding 50)
    diag = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        confidence=0.1,
    )
    act = RecommendedAction(action=ActionType.ESCALATE)
    resp = B2LLMResponse(
        diagnosis=diag,
        action=act,
        confidence=0.1,
        uncertainty_note="test note",
    )
    client = FakeLLMClient(
        responses=[resp],
        budget_guard=guard,
        model_name="fake-gemini",
    )

    res = run_b2(sample_alert, client=client, budget_guard=guard)

    assert res.error_type == "provider_error"
    assert "budget exceeded" in res.action.notes.lower()
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.action.action == ActionType.ESCALATE


def test_b2_prompt_leakage_scan() -> None:
    """Requirement: prompt template contains ZERO forbidden leakage strings (Feature 1.13 scan)."""
    prompt_text, p_hash = load_prompt_template()
    assert len(p_hash) == 64

    # 1. Family names
    for fam in FORBIDDEN_FAMILY_NAMES:
        assert fam.lower() not in prompt_text.lower(), f"Family '{fam}' found in B2 prompt!"

    # 2. Forbidden regex patterns
    for term, pat in FORBIDDEN_TERMS_PATTERNS:
        assert not pat.search(prompt_text), f"Forbidden pattern '{term}' matched in B2 prompt!"

    # 3. Scenario identifiers
    for scn in ["S1", "S2", "S3", "S4", "S5"]:
        assert not re.search(rf"\b{scn}\b", prompt_text, re.IGNORECASE), (
            f"Scenario '{scn}' found in prompt!"
        )


def test_b2_pricing_config() -> None:
    """Requirement: price config in a file with checked_on date and unverified TODO note."""
    cfg = load_pricing_config()
    assert "checked_on" in cfg
    assert cfg.get("unverified") is True
    assert "todo_note" in cfg
    assert "models" in cfg

    # Cost calculation check
    cost = calculate_cost(
        "gemini-2.0-flash", input_tokens=1000, output_tokens=500, pricing_config=cfg
    )
    assert cost > 0.0


def test_b2_multi_trials(sample_alert: AlertEvent) -> None:
    """Requirement: support --trials N with default trials=1."""
    diag = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        confidence=0.1,
    )
    act = RecommendedAction(action=ActionType.ESCALATE)
    resp = B2LLMResponse(
        diagnosis=diag,
        action=act,
        confidence=0.1,
        uncertainty_note="trial test",
    )

    client = FakeLLMClient(responses=[resp, resp, resp], model_name="fake-gemini")
    results = run_b2_trials(sample_alert, trials=3, client=client, scenario_id="S5")

    assert len(results) == 3
    assert [r.trial for r in results] == [1, 2, 3]
