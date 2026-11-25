"""tests/test_graph_state_and_steps.py

Unit tests for agent/graph/state.py and agent/graph/steps.py.

Verifies:
1. Step validation:
   - Unknown tool rejected.
   - Oversize reason (> 300 chars) rejected.
   - Empty or control-character reason rejected.
   - Wrong diagnosis schema rejected.
   - Extra fields rejected on both CallToolStep and ConcludeStep.
   - Discriminated union AgentStep parsing.
   - Tool argument validation at dispatch.
2. Reducer behavior:
   - append_evidence_ids (append-only, order-preserving, deduplication).
   - append_tool_trace (append-only, payload exclusion).
   - Full LangGraph StateGraph reducer and scalar update behavior.
3. Serialization round-trip:
   - State -> JSON -> State round-trip preserves all fields.
4. Feature 2.3 Secret detector integration:
   - Asserts a valid serialized state contains zero secret-looking values.
   - Confirms detector triggers if a secret is injected.
5. State byte cap:
   - Asserts a worst-case 8-tool-call investigation state serializes under 16 KB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from langgraph.graph import StateGraph
from pydantic import ValidationError

from agent.graph.state import (
    MAX_WORST_CASE_STATE_BYTES,
    InvestigationState,
    TerminationReason,
    ToolCallRecordRef,
    append_evidence_ids,
    append_tool_trace,
    create_initial_state,
    get_final_status_for_termination,
    state_from_json,
    state_to_json,
)
from agent.graph.steps import (
    CallToolStep,
    ConcludeStep,
    parse_agent_step,
)
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis
from tools.common.sanitize import redact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        alert_id="alt_orders_pool_001",
        fired_at=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
        service="orders",
        rule_id="rule_db_pool_exhaustion",
        metric="db_pool_in_use",
        threshold=8.0,
        observed_value=10.0,
        window_seconds=60,
        severity="CRITICAL",
        status="firing",
    )


@pytest.fixture
def sample_diagnosis() -> Diagnosis:
    return Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="deploy_orders_v130",
        evidence_ids=["ev_metric_3d81", "ev_log_7c2a", "ev_diff_2e4b"],
        confidence=0.95,
    )


# ---------------------------------------------------------------------------
# 1. Step Validation Tests
# ---------------------------------------------------------------------------
def test_call_tool_step_valid() -> None:
    step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        reason="Checking connection pool in use for orders microservice.",
    )
    assert step.step == "call_tool"
    assert step.tool == "get_metrics"
    assert step.reason == "Checking connection pool in use for orders microservice."


def test_call_tool_step_unknown_tool_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CallToolStep(
            tool="drop_database",
            args={},
            reason="Attempting unauthorized operation",
        )
    assert "Unknown tool 'drop_database'" in str(exc_info.value)


def test_call_tool_step_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CallToolStep.model_validate(
            {
                "step": "call_tool",
                "tool": "get_metrics",
                "args": {},
                "reason": "Valid reason",
                "unauthorized_field": "injected",
            }
        )
    assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not permitted" in str(
        exc_info.value
    )


def test_step_reason_oversize_rejected() -> None:
    oversize = "x" * 301
    with pytest.raises(ValidationError) as exc_info:
        CallToolStep(
            tool="get_metrics",
            args={},
            reason=oversize,
        )
    assert "exceeds 300 characters" in str(exc_info.value)


def test_step_reason_empty_or_whitespace_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CallToolStep(
            tool="get_metrics",
            args={},
            reason="   \n\t  ",
        )
    assert "cannot be empty" in str(exc_info.value)


def test_step_reason_control_characters_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CallToolStep(
            tool="get_metrics",
            args={},
            reason="Checking orders metric\x00with null byte",
        )
    assert "must be plain text without control characters" in str(exc_info.value)


def test_conclude_step_valid(sample_diagnosis: Diagnosis) -> None:
    step = ConcludeStep(
        diagnosis_draft=sample_diagnosis,
        reason="Diagnosis established with pool exhaustion evidence.",
    )
    assert step.step == "conclude"
    assert step.diagnosis_draft.root_cause_service == "orders"
    assert step.diagnosis_draft.confidence == 0.95


def test_conclude_step_wrong_diagnosis_schema_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ConcludeStep.model_validate(
            {
                "step": "conclude",
                "diagnosis_draft": {
                    "root_cause_service": "orders",
                    "root_cause_category": "invalid_category",  # Not in allowed categories
                    "mechanism": "connection_pool_exhaustion",
                    "confidence": 1.5,  # Out of range [0.0, 1.0]
                },
                "reason": "Fault found",
            }
        )
    err = str(exc_info.value)
    assert "root_cause_category" in err or "confidence" in err


def test_conclude_step_extra_fields_rejected(sample_diagnosis: Diagnosis) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ConcludeStep.model_validate(
            {
                "step": "conclude",
                "diagnosis_draft": sample_diagnosis.model_dump(),
                "reason": "Diagnosis complete",
                "extra_injection": "malicious",
            }
        )
    assert "Extra inputs are not permitted" in str(exc_info.value) or "extra_forbidden" in str(
        exc_info.value
    )


def test_agent_step_discriminated_union(sample_diagnosis: Diagnosis) -> None:
    # 1. Parse call_tool
    raw_tool = {
        "step": "call_tool",
        "tool": "search_logs",
        "args": {"service": "orders", "level": "error"},
        "reason": "Search error logs for timeout events.",
    }
    parsed_tool = parse_agent_step(raw_tool)
    assert isinstance(parsed_tool, CallToolStep)
    assert parsed_tool.tool == "search_logs"

    # 2. Parse conclude
    raw_conclude = {
        "step": "conclude",
        "diagnosis_draft": sample_diagnosis.model_dump(),
        "reason": "Orders pool leak confirmed by diff.",
    }
    parsed_conclude = parse_agent_step(raw_conclude)
    assert isinstance(parsed_conclude, ConcludeStep)
    assert parsed_conclude.diagnosis_draft.culprit_deploy_id == "deploy_orders_v130"

    # 3. Invalid discriminator
    with pytest.raises(ValidationError):
        parse_agent_step({"step": "unknown_step", "reason": "invalid"})


def test_call_tool_args_validation_against_registry() -> None:
    # Valid get_metrics args
    step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        reason="Valid metric query",
    )
    validated = step.validate_args_against_tool_schema()
    assert validated.service == "orders"
    assert validated.metric == "db_pool_in_use"

    # Invalid get_metrics args (missing required 'metric')
    bad_step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders"},  # metric is missing
        reason="Missing metric query",
    )
    with pytest.raises(ValidationError):
        bad_step.validate_args_against_tool_schema()


# ---------------------------------------------------------------------------
# 2. Reducer Behavior Tests
# ---------------------------------------------------------------------------
def test_append_evidence_ids_reducer() -> None:
    # Append to empty
    res1 = append_evidence_ids(None, ["ev_1", "ev_2"])
    assert res1 == ["ev_1", "ev_2"]

    # Append new items
    res2 = append_evidence_ids(res1, ["ev_3"])
    assert res2 == ["ev_1", "ev_2", "ev_3"]

    # Deduplicate existing items preserving order
    res3 = append_evidence_ids(res2, ["ev_2", "ev_4", "ev_1"])
    assert res3 == ["ev_1", "ev_2", "ev_3", "ev_4"]

    # Single string update
    res4 = append_evidence_ids(res3, "ev_5")
    assert res4 == ["ev_1", "ev_2", "ev_3", "ev_4", "ev_5"]

    # None / empty update
    res5 = append_evidence_ids(res4, None)
    assert res5 == res4


def test_append_tool_trace_reducer() -> None:
    ref1 = ToolCallRecordRef(
        tool="get_metrics",
        args_summary="service=orders, metric=db_pool_in_use",
        duration_ms=45.2,
        ok=True,
        evidence_id="ev_metric_1234",
    )
    ref2 = {
        "tool": "search_logs",
        "args": {"service": "orders", "level": "error"},
        "duration_ms": 78.1,
        "ok": True,
        "evidence_id": "ev_log_5678",
    }

    # Append from empty
    trace1 = append_tool_trace(None, ref1)
    assert len(trace1) == 1
    assert trace1[0]["tool"] == "get_metrics"
    assert trace1[0]["evidence_id"] == "ev_metric_1234"

    # Append dict
    trace2 = append_tool_trace(trace1, ref2)
    assert len(trace2) == 2
    assert trace2[1]["tool"] == "search_logs"
    assert "service=orders" in trace2[1]["args_summary"]


def test_langgraph_stategraph_reducer_integration(sample_alert: AlertEvent) -> None:
    """Tests LangGraph StateGraph execution with InvestigationState and explicit reducers."""
    builder = StateGraph(InvestigationState)

    def node_start(state: InvestigationState) -> dict[str, Any]:
        return {
            "evidence_ids": ["ev_init"],
            "tool_trace": [
                ToolCallRecordRef(tool="get_metrics", args_summary="gateway", ok=True)
            ],
            "step_count": 1,
            "remaining_calls": 7,
            "messages_summary": "Step 1: Ingress metrics checked.",
        }

    def node_second(state: InvestigationState) -> dict[str, Any]:
        return {
            "evidence_ids": ["ev_orders", "ev_init"],  # ev_init should be deduplicated
            "tool_trace": [
                ToolCallRecordRef(tool="search_logs", args_summary="orders", ok=True)
            ],
            "step_count": 2,
            "remaining_calls": 6,
            "messages_summary": "Step 2: Orders logs checked.",
            "status": "needs_review",
        }

    builder.add_node("node_start", node_start)
    builder.add_node("node_second", node_second)
    builder.set_entry_point("node_start")
    builder.add_edge("node_start", "node_second")
    builder.set_finish_point("node_second")

    graph = builder.compile()

    initial = create_initial_state(
        run_id="run_test_01",
        tenant_id="tenant-default",
        incident_id="inc_20261124_01",
        alert=sample_alert,
    )

    final_state = graph.invoke(initial)

    # Reducer behavior verified: list fields appended and deduplicated
    assert final_state["evidence_ids"] == ["ev_init", "ev_orders"]
    assert len(final_state["tool_trace"]) == 2
    assert final_state["tool_trace"][0]["tool"] == "get_metrics"
    assert final_state["tool_trace"][1]["tool"] == "search_logs"

    # Scalar last-write-wins verified
    assert final_state["step_count"] == 2
    assert final_state["remaining_calls"] == 6
    assert final_state["messages_summary"] == "Step 2: Orders logs checked."
    assert final_state["status"] == "needs_review"


# ---------------------------------------------------------------------------
# 3. Serialization Round-Trip Tests
# ---------------------------------------------------------------------------
def test_state_serialization_round_trip(sample_alert: AlertEvent, sample_diagnosis: Diagnosis) -> None:
    state = create_initial_state(
        run_id="run_abc123",
        tenant_id="tenant-default",
        incident_id="inc_xyz789",
        alert=sample_alert,
        max_calls=8,
    )
    state["evidence_ids"] = ["ev_1", "ev_2"]
    state["tool_trace"] = [
        ToolCallRecordRef(
            tool="get_metrics",
            args_summary="service=orders",
            duration_ms=50.0,
            ok=True,
            evidence_id="ev_1",
        ).model_dump()
    ]
    state["draft_diagnosis"] = sample_diagnosis.model_dump()
    state["messages_summary"] = "Discovered connection pool timeout in orders log."
    state["termination_reason"] = TerminationReason.CONCLUDED_BY_MODEL.value
    state["status"] = "needs_review"

    # Serialize to JSON
    raw_json = state_to_json(state)
    assert isinstance(raw_json, str)

    # Deserialize back
    restored = state_from_json(raw_json)

    assert restored["run_id"] == "run_abc123"
    assert restored["tenant_id"] == "tenant-default"
    assert restored["incident_id"] == "inc_xyz789"
    assert restored["status"] == "needs_review"
    assert restored["evidence_ids"] == ["ev_1", "ev_2"]
    assert len(restored["tool_trace"]) == 1
    assert restored["tool_trace"][0]["evidence_id"] == "ev_1"
    assert restored["draft_diagnosis"]["root_cause_service"] == "orders"
    assert restored["termination_reason"] == "concluded_by_model"


# ---------------------------------------------------------------------------
# 4. Feature 2.3 Secret Detection Tests
# ---------------------------------------------------------------------------
def test_serialized_state_clean_of_secrets(sample_alert: AlertEvent, sample_diagnosis: Diagnosis) -> None:
    """Verifies that a serialized state sample triggers zero redactions under Feature 2.3."""
    state = create_initial_state(
        run_id="run_clean_99",
        tenant_id="tenant-default",
        incident_id="inc_clean_99",
        alert=sample_alert,
    )
    state["evidence_ids"] = ["ev_metric_1234", "ev_log_5678"]
    state["tool_trace"] = [
        ToolCallRecordRef(
            tool="get_metrics",
            args_summary="service=orders, metric=db_pool_in_use",
            ok=True,
            evidence_id="ev_metric_1234",
        ).model_dump()
    ]
    state["messages_summary"] = "Checked connection pool metric and found spike to 10 connections."
    state["draft_diagnosis"] = sample_diagnosis.model_dump()

    serialized = state_to_json(state)

    # Feature 2.3 secret detector (redact) should find ZERO secrets in clean state
    redacted = redact(serialized)
    assert "[REDACTED:" not in redacted
    assert redacted == serialized


def test_serialized_state_detects_injected_secrets(sample_alert: AlertEvent) -> None:
    """Verifies that if a secret is mistakenly stored in state, redact detects and masks it."""
    state = create_initial_state(
        run_id="run_leak_01",
        tenant_id="tenant-default",
        incident_id="inc_leak_01",
        alert=sample_alert,
    )
    # Inject bearer token into messages_summary
    state["messages_summary"] = "Investigated with Bearer secret_api_token_xyz123abc456"

    serialized = state_to_json(state)
    redacted = redact(serialized)

    assert "[REDACTED:BEARER_TOKEN]" in redacted
    assert "secret_api_token_xyz123abc456" not in redacted


# ---------------------------------------------------------------------------
# 5. State Byte Cap Test (Worst-Case Run)
# ---------------------------------------------------------------------------
def test_worst_case_state_size_under_byte_cap(sample_alert: AlertEvent, sample_diagnosis: Diagnosis) -> None:
    """Asserts that an 8-tool-call worst-case run serializes well under MAX_WORST_CASE_STATE_BYTES (16 KB)."""
    state = create_initial_state(
        run_id="run_worst_case_001",
        tenant_id="tenant-enterprise-prod-01",
        incident_id="inc_20261124_critical_pool_leak",
        alert=sample_alert,
        max_calls=8,
    )

    evidence_ids = []
    tool_trace = []
    messages: list[str] = []

    tools = ["get_metrics", "search_logs", "get_trace", "list_deploys", "get_deploy_diff"]

    for i in range(8):
        tool = tools[i % len(tools)]
        ev_id = f"ev_{tool}_{i:04x}a1b2c3d4"
        evidence_ids.append(ev_id)

        # Realistic maximum ~200 char args summary
        args_summary = f"service=orders, metric=db_pool_in_use, step={i}, window_start=2026-11-24T12:00:00Z"
        ref = ToolCallRecordRef(
            tool=tool,
            args_summary=args_summary,
            duration_ms=124.5,
            ok=True,
            evidence_id=ev_id,
            approx_tokens=350,
        )
        tool_trace.append(ref.model_dump())
        messages.append(f"Turn {i+1}: Checked {tool} on orders, discovered evidence {ev_id}.")

    state["evidence_ids"] = evidence_ids
    state["tool_trace"] = tool_trace
    state["messages_summary"] = "\n".join(messages)
    state["step_count"] = 8
    state["remaining_calls"] = 0
    state["draft_diagnosis"] = sample_diagnosis.model_dump()
    state["termination_reason"] = TerminationReason.CONCLUDED_BY_MODEL.value
    state["status"] = "needs_review"

    serialized = state_to_json(state)
    byte_size = len(serialized.encode("utf-8"))

    # Assert strict compliance with documented byte cap
    assert byte_size < MAX_WORST_CASE_STATE_BYTES, (
        f"State size {byte_size} bytes exceeds maximum allowed limit {MAX_WORST_CASE_STATE_BYTES} bytes"
    )
    # Typically around 3.5 - 5 KB
    assert byte_size < 8192, f"State size {byte_size} is unexpectedly large (>8 KB)"


# ---------------------------------------------------------------------------
# 6. Termination Reason & Status Mapping Tests
# ---------------------------------------------------------------------------
def test_termination_status_mapping() -> None:
    assert get_final_status_for_termination(TerminationReason.CONCLUDED_BY_MODEL) == "needs_review"
    assert get_final_status_for_termination(TerminationReason.BUDGET_EXHAUSTED) == "escalated"
    assert get_final_status_for_termination(TerminationReason.STEP_CAP_REACHED) == "escalated"
    assert get_final_status_for_termination(TerminationReason.TIMEOUT_EXCEEDED) == "escalated"
    assert get_final_status_for_termination(TerminationReason.TOKEN_BUDGET_EXCEEDED) == "escalated"
    assert get_final_status_for_termination(TerminationReason.PROVIDER_ERROR_EXHAUSTED) == "escalated"
    assert get_final_status_for_termination(TerminationReason.INVALID_STEP_CAP_REACHED) == "escalated"
    assert get_final_status_for_termination("unknown_reason") == "escalated"
