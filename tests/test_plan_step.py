"""tests/test_plan_step.py

Unit tests for agent/graph/nodes/plan_step.py using FakeLLMClient.

Verifies:
1. Valid call_tool step generation:
   - Updates state with step_count, last_step_decision, tokens, and messages_summary.
   - Emits "agent_step" run_event without raw arguments.
2. Valid conclude step generation:
   - Updates state with draft_diagnosis, sets status="needs_review",
     termination_reason="concluded_by_model".
3. Schema failure then repair success:
   - Initial call returns schema validation error.
   - Repair prompt is sent with validation error summary.
   - Repaired response succeeds; tokens from both turns are accounted for.
4. Double failure increments invalid counter:
   - Both initial and repair calls fail schema validation.
   - invalid_step_count is incremented.
   - When invalid_step_count reaches cap (3), terminates with status="escalated"
     and termination_reason="invalid_step_cap_reached".
5. Provider error path:
   - Persistent provider error / timeout terminates with status="escalated"
     and termination_reason="provider_error_exhausted".
   - Never converts failure into a diagnosis.
6. Forced conclusion at budget 0:
   - When remaining_calls <= 0, planner routes straight to conclude with
     forced-conclusion prompt variant and ConcludeStep schema.
7. Token guard:
   - TokenBudgetGuard raises BudgetExceededError; node routes to termination
     with status="escalated" and termination_reason="token_budget_exceeded".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent.graph.nodes.plan_step import plan_step_node
from agent.graph.state import (
    InvestigationState,
    TerminationReason,
    create_initial_state,
)
from agent.graph.steps import (
    CallToolStep,
    ConcludeStep,
)
from agent.llm.fake import FakeLLMClient
from agent.llm.interface import (
    LLMError,
    StructuredResult,
    TokenBudgetGuard,
    TokenUsage,
)
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis


# ---------------------------------------------------------------------------
# Fixtures & Mock Service
# ---------------------------------------------------------------------------
class MockRunService:
    """Mock RunService capturing audit events and error recordings in-memory."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

    def append_event(
        self,
        ctx: Any,
        run_id: Any,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.events.append(
            {
                "run_id": str(run_id),
                "tenant_id": str(ctx.tenant_id),
                "event_type": event_type,
                "actor": actor,
                "payload": payload or {},
            }
        )
        return None

    def record_error(
        self,
        ctx: Any,
        run_id: Any,
        error: dict[str, Any] | str,
        actor: str = "system",
        target_status: str | None = None,
    ) -> Any:
        self.errors.append(
            {
                "run_id": str(run_id),
                "error": error,
                "target_status": target_status,
            }
        )
        return None


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
def base_state(sample_alert: AlertEvent) -> InvestigationState:
    run_id = str(uuid4())
    tenant_id = str(uuid4())
    incident_id = str(uuid4())
    return create_initial_state(
        run_id=run_id,
        tenant_id=tenant_id,
        incident_id=incident_id,
        alert=sample_alert,
        max_calls=8,
    )


# ---------------------------------------------------------------------------
# 1. Valid call_tool Step
# ---------------------------------------------------------------------------
def test_valid_call_tool_step(base_state: InvestigationState) -> None:
    expected_step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        reason="Querying database pool usage for orders service to test leak hypothesis.",
    )
    fake_llm = FakeLLMClient(
        responses=[
            StructuredResult[Any](
                parsed=expected_step,
                raw_text=expected_step.model_dump_json(),
                token_usage=TokenUsage(input_tokens=150, output_tokens=40, total_tokens=190),
            )
        ]
    )
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    assert updates["step_count"] == 1
    assert updates["last_step_decision"]["step"] == "call_tool"
    assert updates["last_step_decision"]["tool"] == "get_metrics"
    assert updates["total_tokens_consumed"] == 190
    assert "Turn 1: call_tool(get_metrics)" in updates["messages_summary"]

    # Verify run_event was emitted through RunService
    assert len(mock_run_service.events) == 1
    event = mock_run_service.events[0]
    assert event["event_type"] == "agent_step"
    assert event["actor"] == "investigator"
    assert event["payload"]["step_kind"] == "call_tool"
    assert event["payload"]["tool"] == "get_metrics"
    assert event["payload"]["reason"] == expected_step.reason
    assert "args" not in event["payload"]  # Raw args strictly excluded!


# ---------------------------------------------------------------------------
# 2. Valid conclude Step
# ---------------------------------------------------------------------------
def test_valid_conclude_step(base_state: InvestigationState) -> None:
    diagnosis = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="deploy_orders_v130",
        evidence_ids=["ev_metric_01", "ev_log_02"],
        confidence=0.95,
    )
    expected_step = ConcludeStep(
        diagnosis_draft=diagnosis,
        reason="Sufficient telemetry confirms connection pool leak introduced in deploy v130.",
    )
    fake_llm = FakeLLMClient(
        responses=[
            StructuredResult[Any](
                parsed=expected_step,
                raw_text=expected_step.model_dump_json(),
                token_usage=TokenUsage(input_tokens=200, output_tokens=80, total_tokens=280),
            )
        ]
    )
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    assert updates["step_count"] == 1
    assert updates["last_step_decision"]["step"] == "conclude"
    assert updates["draft_diagnosis"]["root_cause_service"] == "orders"
    assert updates["draft_diagnosis"]["confidence"] == 0.95
    assert updates["status"] == "needs_review"
    assert updates["termination_reason"] == TerminationReason.CONCLUDED_BY_MODEL.value
    assert updates["total_tokens_consumed"] == 280

    assert len(mock_run_service.events) == 1
    event = mock_run_service.events[0]
    assert event["payload"]["step_kind"] == "conclude"
    assert event["payload"]["tool"] is None


# ---------------------------------------------------------------------------
# 3. Schema Failure then Repair Success
# ---------------------------------------------------------------------------
def test_schema_failure_then_repair_success(base_state: InvestigationState) -> None:
    # 1st call fails schema validation
    initial_failure = StructuredResult[Any](
        error=LLMError(
            error_type="schema_validation_failed",
            message="Field 'reason' exceeds 300 characters",
        ),
        raw_text='{"step": "call_tool", "tool": "get_metrics", "reason": "too long..."}',
        token_usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
    )
    # 2nd call (repair) succeeds
    repaired_step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders", "metric": "cpu_utilization"},
        reason="Repaired: Checking CPU utilization for orders service.",
    )
    repaired_success = StructuredResult[Any](
        parsed=repaired_step,
        raw_text=repaired_step.model_dump_json(),
        token_usage=TokenUsage(input_tokens=180, output_tokens=40, total_tokens=220),
    )

    fake_llm = FakeLLMClient(responses=[initial_failure, repaired_success])
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    # Assert repair was invoked and succeeded
    assert len(fake_llm.call_history) == 2
    repair_call_messages = fake_llm.call_history[1]["messages"]
    # Check that repair message asked to correct the validation error
    last_msg = repair_call_messages[-1]
    assert last_msg.role == "user"
    assert "Your previous response failed schema validation" in last_msg.content
    assert "Field 'reason' exceeds 300 characters" in last_msg.content

    # State reflects valid repaired step and combined token usage
    assert updates["step_count"] == 1
    assert updates["last_step_decision"]["step"] == "call_tool"
    assert updates["last_step_decision"]["reason"] == repaired_step.reason
    assert updates["total_tokens_consumed"] == 370  # 150 + 220


# ---------------------------------------------------------------------------
# 4. Double Failure Increments Invalid Counter (and terminates at 3)
# ---------------------------------------------------------------------------
def test_double_failure_increments_invalid_counter(base_state: InvestigationState) -> None:
    failure1 = StructuredResult[Any](
        error=LLMError(error_type="schema_validation_failed", message="Unknown tool 'fake_tool'"),
        raw_text='{"step": "call_tool", "tool": "fake_tool"}',
        token_usage=TokenUsage(total_tokens=100),
    )
    failure2 = StructuredResult[Any](
        error=LLMError(error_type="schema_validation_failed", message="Still invalid tool"),
        raw_text='{"step": "call_tool", "tool": "fake_tool"}',
        token_usage=TokenUsage(total_tokens=120),
    )

    fake_llm = FakeLLMClient(responses=[failure1, failure2])
    base_state["invalid_step_count"] = 0

    updates = plan_step_node(state=base_state, llm_client=fake_llm)

    assert updates["invalid_step_count"] == 1
    assert updates["total_tokens_consumed"] == 220
    assert updates["last_error"]["code"] == "schema_validation_failed"
    assert "Still invalid tool" in updates["last_error"]["message"]
    assert "status" not in updates  # Has not reached cap yet


def test_invalid_step_cap_reached_terminates(base_state: InvestigationState) -> None:
    failure1 = StructuredResult[Any](
        error=LLMError(error_type="schema_validation_failed", message="Parse error"),
        raw_text="invalid json",
        token_usage=TokenUsage(total_tokens=50),
    )
    failure2 = StructuredResult[Any](
        error=LLMError(error_type="schema_validation_failed", message="Parse error again"),
        raw_text="invalid json",
        token_usage=TokenUsage(total_tokens=50),
    )

    fake_llm = FakeLLMClient(responses=[failure1, failure2])
    # Set invalid_step_count to 2 so next failure hits cap of 3
    base_state["invalid_step_count"] = 2
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    assert updates["invalid_step_count"] == 3
    assert updates["status"] == "escalated"
    assert updates["termination_reason"] == TerminationReason.INVALID_STEP_CAP_REACHED.value
    assert updates["last_error"]["code"] == "schema_validation_failed"

    # Verify audit event emitted
    assert len(mock_run_service.events) == 1
    assert mock_run_service.events[0]["event_type"] == "invalid_steps_escalated"


# ---------------------------------------------------------------------------
# 5. Provider Error Path
# ---------------------------------------------------------------------------
def test_provider_error_terminates_without_diagnosis(base_state: InvestigationState) -> None:
    provider_err = StructuredResult[Any](
        error=LLMError(
            error_type="provider_error",
            message="Gemini upstream 503 Unavailable after 3 retries",
            details={"status_code": 503},
        ),
        token_usage=TokenUsage(total_tokens=25),
    )
    fake_llm = FakeLLMClient(responses=[provider_err])
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    assert updates["status"] == "escalated"
    assert updates["termination_reason"] == TerminationReason.PROVIDER_ERROR_EXHAUSTED.value
    assert updates["last_error"]["code"] == "provider_error"
    assert "Gemini upstream 503" in updates["last_error"]["message"]
    # Crucial: never convert provider error into a diagnosis!
    assert "draft_diagnosis" not in updates

    assert len(mock_run_service.events) == 1
    assert mock_run_service.events[0]["event_type"] == "provider_error_escalated"


# ---------------------------------------------------------------------------
# 6. Forced Conclusion at Budget 0
# ---------------------------------------------------------------------------
def test_forced_conclusion_at_budget_zero(base_state: InvestigationState) -> None:
    # Set remaining_calls = 0 to trigger forced conclusion
    base_state["remaining_calls"] = 0

    diagnosis = Diagnosis(
        root_cause_service=None,
        root_cause_category="unknown",
        mechanism="unknown",
        culprit_deploy_id=None,
        evidence_ids=[],
        confidence=0.0,
    )
    conclude_step = ConcludeStep(
        diagnosis_draft=diagnosis,
        reason="Budget exhausted (0 calls remaining); evidence insufficient to confirm root cause.",
    )
    fake_llm = FakeLLMClient(
        responses=[
            StructuredResult[Any](
                parsed=conclude_step,
                raw_text=conclude_step.model_dump_json(),
                token_usage=TokenUsage(input_tokens=120, output_tokens=60, total_tokens=180),
            )
        ]
    )

    updates = plan_step_node(state=base_state, llm_client=fake_llm)

    # Verify LLM was prompted with ConcludeStep directly (not AgentStep)
    assert len(fake_llm.call_history) == 1
    call = fake_llm.call_history[0]
    assert call["schema"] is ConcludeStep

    # Verify user message is forced conclusion variant without tool specs
    user_prompt = call["messages"][-1].content
    assert "Tool Budget Status: Exhausted (0 remaining allowed queries)" in user_prompt
    assert "Authorized Investigation Tool Specifications" not in user_prompt

    # Updates reflect concluded investigation
    assert updates["status"] == "needs_review"
    assert updates["termination_reason"] == TerminationReason.CONCLUDED_BY_MODEL.value
    assert updates["draft_diagnosis"]["root_cause_category"] == "unknown"


# ---------------------------------------------------------------------------
# 7. Token Guard Path
# ---------------------------------------------------------------------------
def test_token_guard_budget_exceeded(base_state: InvestigationState) -> None:
    # Set guard with low limit
    guard = TokenBudgetGuard(max_tokens=100)
    # Pre-consume 90 tokens
    guard.check_and_consume(90)

    # Next call consumes 50 tokens -> will raise BudgetExceededError
    fake_llm = FakeLLMClient(
        budget_guard=guard,
        responses=[
            StructuredResult[Any](
                parsed=CallToolStep(
                    tool="get_metrics",
                    args={"service": "orders"},
                    reason="Test reason",
                ),
                token_usage=TokenUsage(total_tokens=50),
            )
        ],
    )
    mock_run_service = MockRunService()

    updates = plan_step_node(
        state=base_state,
        llm_client=fake_llm,
        run_service=mock_run_service,  # type: ignore[arg-type]
    )

    assert updates["status"] == "escalated"
    assert updates["termination_reason"] == TerminationReason.TOKEN_BUDGET_EXCEEDED.value
    assert updates["last_error"]["code"] == "token_budget_exceeded"
    assert updates["last_error"]["limit"] == 100

    assert len(mock_run_service.events) == 1
    assert mock_run_service.events[0]["event_type"] == "token_budget_escalated"
