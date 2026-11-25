"""tests/test_step_schema_v2.py

Table-driven tests for Feature 6 Step Schema V2 and StepValidator:
1. Every StepRejection code reproduced deterministically:
   - unknown_hypothesis
   - hypothesis_already_refuted
   - invalid_evidence_id (fabricated evidence ID & evidence not in run)
   - refuted_without_contradiction
   - supported_without_evidence
   - ledger_capacity_exceeded (adding a 5th hypothesis)
   - too_many_ops (> 6 operations)
   - invalid_confidence_format (direct float confidence manipulation)
2. Backwards compatibility: V1 steps continue to parse and validate under version="v1".
3. FlatAgentStepV2 compatibility with Gemini structured output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent.graph.ledger import (
    HypothesisLedger,
    add_hypothesis,
    link_evidence,
    set_status,
)
from agent.graph.steps import (
    CallToolStep,
    CallToolStepV2,
    ConcludeStep,
    ConcludeStepV2,
    FlatAgentStepV2,
    StepRejection,
    StepValidator,
    UpdateHypothesesStepV2,
    parse_agent_step,
)
from evals.graders.models import Diagnosis, EvidenceItem

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_incident_window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 11, 24, 11, 45, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 24, 12, 15, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_evidence_store(sample_incident_window) -> dict[str, EvidenceItem]:
    return {
        "ev_logs_valid": EvidenceItem(
            evidence_id="ev_logs_valid",
            timestamp=datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
            source="logs",
        ),
        "ev_metrics_valid": EvidenceItem(
            evidence_id="ev_metrics_valid",
            timestamp=datetime(2026, 11, 24, 12, 2, 0, tzinfo=timezone.utc),
            source="prometheus",
        ),
    }


@pytest.fixture
def sample_ledger() -> HypothesisLedger:
    ledger = HypothesisLedger()
    ledger, hyp = add_hypothesis(
        ledger=ledger,
        statement="Orders pool exhaustion due to leak in v1.3.0",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
    )
    return ledger


# ---------------------------------------------------------------------------
# 1. Table-Driven StepValidator Rejections Tests
# ---------------------------------------------------------------------------

REJECTION_TEST_CASES = [
    # 1. Unknown hypothesis on call_tool
    (
        CallToolStepV2(
            step="call_tool",
            tool="search_logs",
            tests_hypothesis_id="hyp_unknown_99",
            expected_if_true="Logs show errors",
            expected_if_false="Logs clean",
            reason="Testing unknown hypothesis",
        ),
        "base_state",
        "unknown_hypothesis",
        "tests_hypothesis_id",
    ),
    # 2. Testing already-refuted hypothesis on call_tool
    (
        CallToolStepV2(
            step="call_tool",
            tool="search_logs",
            tests_hypothesis_id="hyp_01",
            expected_if_true="Logs show errors",
            expected_if_false="Logs clean",
            reason="Testing refuted hypothesis",
        ),
        "refuted_state",
        "hypothesis_already_refuted",
        "tests_hypothesis_id",
    ),
    # 3. Fabricated / out-of-run evidence ID on link_evidence
    (
        UpdateHypothesesStepV2(
            step="update_hypotheses",
            operations=[
                {
                    "op": "link_evidence",
                    "hypothesis_id": "hyp_01",
                    "evidence_id": "ev_fabricated_9999",
                    "relation": "supports",
                }
            ],
            reason="Linking non-existent evidence",
        ),
        "base_state",
        "invalid_evidence_id",
        "operations[0].evidence_id",
    ),
    # 4. Marking hypothesis supported with ZERO supporting evidence
    (
        UpdateHypothesesStepV2(
            step="update_hypotheses",
            operations=[
                {
                    "op": "set_status",
                    "hypothesis_id": "hyp_01",
                    "status": "supported",
                }
            ],
            reason="Supporting without evidence",
        ),
        "base_state",
        "supported_without_evidence",
        "operations[0].status",
    ),
    # 5. Marking hypothesis refuted with ZERO contradicting evidence
    (
        UpdateHypothesesStepV2(
            step="update_hypotheses",
            operations=[
                {
                    "op": "set_status",
                    "hypothesis_id": "hyp_01",
                    "status": "refuted",
                }
            ],
            reason="Refuting without contradiction",
        ),
        "base_state",
        "refuted_without_contradiction",
        "operations[0].status",
    ),
    # 6. Adding 5th hypothesis when ledger is at capacity (4)
    (
        UpdateHypothesesStepV2(
            step="update_hypotheses",
            operations=[
                {
                    "op": "add",
                    "statement": "Fifth candidate cause",
                    "category": "dependency_failure",
                    "mechanism": "dependency_timeout",
                    "service": "inventory",
                    "confidence_label": "low",
                }
            ],
            reason="Adding hypothesis beyond cap",
        ),
        "full_state",
        "ledger_capacity_exceeded",
        "operations[0]",
    ),
    # 7. Model passing direct float confidence in add operation
    (
        UpdateHypothesesStepV2(
            step="update_hypotheses",
            operations=[
                {
                    "op": "add",
                    "statement": "Valid statement",
                    "category": "dependency_failure",
                    "mechanism": "dependency_timeout",
                    "service": "inventory",
                    "confidence": 0.85,  # Forbidden direct float
                }
            ],
            reason="Attempting direct float confidence manipulation",
        ),
        "base_state",
        "invalid_confidence_format",
        "operations[0].confidence",
    ),
    # 8. More than 6 ops in a single update_hypotheses turn
    (
        UpdateHypothesesStepV2.model_construct(
            step="update_hypotheses",
            operations=[
                {"op": "add", "statement": f"Hyp {i}", "category": "bad_deploy", "mechanism": "none"}
                for i in range(7)
            ],
            reason="Too many ops",
        ),
        "base_state",
        "too_many_ops",
        "operations",
    ),
    # 9. Conclude with unknown leading hypothesis
    (
        ConcludeStepV2(
            step="conclude",
            leading_hypothesis_id="hyp_ghost_01",
            diagnosis_draft=Diagnosis(
                root_cause_service="orders",
                root_cause_category="bad_deploy",
                mechanism="connection_pool_exhaustion",
                evidence_ids=["ev_logs_valid"],
                confidence=0.9,
            ),
            reason="Concluding with unknown hypothesis",
        ),
        "base_state",
        "unknown_hypothesis",
        "leading_hypothesis_id",
    ),
    # 10. Conclude with refuted leading hypothesis
    (
        ConcludeStepV2(
            step="conclude",
            leading_hypothesis_id="hyp_01",
            diagnosis_draft=Diagnosis(
                root_cause_service="orders",
                root_cause_category="bad_deploy",
                mechanism="connection_pool_exhaustion",
                evidence_ids=["ev_logs_valid"],
                confidence=0.9,
            ),
            reason="Concluding with refuted hypothesis",
        ),
        "refuted_state",
        "hypothesis_already_refuted",
        "leading_hypothesis_id",
    ),
]


@pytest.mark.parametrize(
    "step_obj,state_type,expected_code,expected_field",
    REJECTION_TEST_CASES,
)
def test_step_validator_rejections(
    step_obj: Any,
    state_type: str,
    expected_code: str,
    expected_field: str,
    sample_ledger: HypothesisLedger,
    sample_evidence_store: dict[str, EvidenceItem],
    sample_incident_window: tuple[datetime, datetime],
) -> None:
    """Verifies that each invalid step produces the exact typed StepRejection code and field."""
    # Build state fixture
    if state_type == "base_state":
        state = {
            "hypotheses_ledger": sample_ledger,
            "evidence_ids": ["ev_logs_valid", "ev_metrics_valid"],
        }
    elif state_type == "refuted_state":
        refuted_ledger, _ = set_status(
            link_evidence(sample_ledger, "hyp_01", "ev_logs_valid", relation="contradicts")[0],
            "hyp_01",
            "refuted",
        )
        state = {
            "hypotheses_ledger": refuted_ledger,
            "evidence_ids": ["ev_logs_valid"],
        }
    elif state_type == "full_state":
        full_ledger = sample_ledger
        for i in range(2, 5):
            full_ledger, _ = add_hypothesis(
                full_ledger, f"Cause {i}", "bad_deploy", "connection_pool_exhaustion"
            )
        state = {
            "hypotheses_ledger": full_ledger,
            "evidence_ids": [],
        }
    else:
        state = {}

    rejection = StepValidator.validate_step(
        step=step_obj,
        state=state,
        evidence_store=sample_evidence_store,
        incident_window=sample_incident_window,
    )

    assert rejection is not None
    assert isinstance(rejection, StepRejection)
    assert rejection.code == expected_code
    assert rejection.field == expected_field
    assert len(rejection.hint) > 0


# ---------------------------------------------------------------------------
# 2. Valid Steps Pass Validator
# ---------------------------------------------------------------------------

def test_valid_call_tool_step_passes_validator(
    sample_ledger: HypothesisLedger,
    sample_evidence_store: dict[str, EvidenceItem],
    sample_incident_window: tuple[datetime, datetime],
) -> None:
    """Valid CallToolStep targeting open hypothesis passes without rejection."""
    step = CallToolStepV2(
        step="call_tool",
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        tests_hypothesis_id="hyp_01",
        expected_if_true="db_pool_in_use > 7.0",
        expected_if_false="db_pool_in_use <= 7.0",
        reason="Querying database connection pool utilization",
    )
    state = {"hypotheses_ledger": sample_ledger}

    rejection = StepValidator.validate_step(
        step=step,
        state=state,
        evidence_store=sample_evidence_store,
        incident_window=sample_incident_window,
    )
    assert rejection is None


def test_valid_update_hypotheses_passes_validator(
    sample_ledger: HypothesisLedger,
    sample_evidence_store: dict[str, EvidenceItem],
    sample_incident_window: tuple[datetime, datetime],
) -> None:
    """Valid UpdateHypothesesStep linking valid evidence passes without rejection."""
    step = UpdateHypothesesStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "link_evidence",
                "hypothesis_id": "hyp_01",
                "evidence_id": "ev_logs_valid",
                "relation": "supports",
            }
        ],
        reason="Linking verified log evidence to hyp_01",
    )
    state = {
        "hypotheses_ledger": sample_ledger,
        "evidence_ids": ["ev_logs_valid"],
    }

    rejection = StepValidator.validate_step(
        step=step,
        state=state,
        evidence_store=sample_evidence_store,
        incident_window=sample_incident_window,
    )
    assert rejection is None


# ---------------------------------------------------------------------------
# 3. V1 Steps Backwards Compatibility Tests
# ---------------------------------------------------------------------------

def test_v1_steps_validate_under_v1_flag() -> None:
    """V1 CallToolStep and ConcludeStep parse and validate under version='v1' flag."""
    raw_tool = {
        "step": "call_tool",
        "tool": "get_metrics",
        "args": {"service": "orders"},
        "reason": "V1 metrics check",
    }
    step_v1 = parse_agent_step(raw_tool, version="v1")
    assert isinstance(step_v1, CallToolStep)
    assert step_v1.tool == "get_metrics"

    raw_conclude = {
        "step": "conclude",
        "diagnosis_draft": {
            "root_cause_service": "orders",
            "root_cause_category": "bad_deploy",
            "mechanism": "connection_pool_exhaustion",
            "evidence_ids": ["ev_1"],
            "confidence": 0.9,
        },
        "reason": "V1 diagnosis conclusion",
    }
    conclude_v1 = parse_agent_step(raw_conclude, version="v1")
    assert isinstance(conclude_v1, ConcludeStep)
    assert conclude_v1.diagnosis_draft.root_cause_category == "bad_deploy"


# ---------------------------------------------------------------------------
# 4. FlatAgentStepV2 Compatibility Test
# ---------------------------------------------------------------------------

def test_flat_agent_step_parses_cleanly_into_v2_steps() -> None:
    """FlatAgentStepV2 model-facing schema validates JSON from Gemini without polymorphic rejection."""
    flat_tool = FlatAgentStepV2(
        step="call_tool",
        tool="search_logs",
        args={"service": "orders", "query": "exhausted"},
        tests_hypothesis_id="hyp_01",
        expected_if_true="Connection pool timeout errors appear",
        expected_if_false="No errors appear",
        reason="Check logs for pool exhaustion errors",
    )
    assert flat_tool.step == "call_tool"
    assert flat_tool.tool == "search_logs"

    # Can be converted/parsed by parse_agent_step
    parsed = parse_agent_step(flat_tool.model_dump(), version="v2")
    assert isinstance(parsed, CallToolStepV2)
    assert parsed.tests_hypothesis_id == "hyp_01"
