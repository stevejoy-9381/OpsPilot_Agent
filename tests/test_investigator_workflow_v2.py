"""tests/test_investigator_workflow_v2.py

Comprehensive tests for Feature 6 LangGraph Workflow Integration:
1. Normal 3-step trajectory with FakeLLMClient:
   plan_step -> call_tool -> record -> plan_step -> update_ledger -> plan_step -> conclude -> finalize -> END.
2. Ledger op rejected then repaired:
   model proposes an invalid ledger operation (violating StepValidator rules),
   receives a typed StepRejection hint, repairs it on next attempt, and continues.
3. Step cap termination:
   enforces MAX_STEPS = 2 * MAX_TOOL_CALLS + 4 (default 20); update_hypotheses steps
   do NOT consume tool budget but count toward the global step cap, terminating with step_cap_reached.
4. Compaction keeps cited IDs:
   verifies that prompt context compaction NEVER drops or compacts evidence IDs cited in the hypothesis ledger.
5. Router refuses conclude before first ledger update:
   verifies router refuses conclude if tool_trace is non-empty and ledger_update_count == 0.
6. Worst-case token cap test:
   4 hypotheses and 8 tool calls assemble cleanly under the 4,000 token per-turn budget.
7. Interleaved events audit table:
   demonstrates interleaved execution of tool_call, agent_step, hypothesis_added,
   evidence_linked, and diagnosis events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agent.graph.context import DEFAULT_MAX_PROMPT_TOKENS, build_investigator_context
from agent.graph.graph import (
    build_investigator_graph,
    route_after_plan_step,
    run_investigation_graph,
)
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.state import (
    TerminationReason,
    ToolCallRecordRef,
    create_initial_state,
)
from agent.graph.steps import (
    FlatAgentStepV2,
)
from agent.llm.fake import FakeLLMClient
from apps.api.domain.context import TenantContext
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis
from tools.common.models import ToolSuccess, Window
from tools.registry import GetMetricsInput, ToolRegistry


# ---------------------------------------------------------------------------
# Test Mocks for Service and Repository
# ---------------------------------------------------------------------------
class MockRunRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.diagnoses: dict[UUID, Any] = {}
        self.observations: dict[UUID, list[Any]] = {}

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        seq = len(self.events) + 1
        event = {
            "run_id": str(run_id),
            "tenant_id": str(ctx.tenant_id),
            "seq": seq,
            "event_type": event_type,
            "actor": actor,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.events.append(event)

        class EventRef:
            pass

        ref = EventRef()
        ref.seq = seq  # type: ignore[attr-defined]
        return ref

    def save_diagnosis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        diagnosis_data: dict[str, Any],
        status: str = "draft",
    ) -> Any:
        self.diagnoses[run_id] = diagnosis_data
        return diagnosis_data

    def get_by_id(self, ctx: TenantContext, run_id: UUID) -> Any:
        class FakeRun:
            id = run_id
            version = 1
            status = "investigating"
        return FakeRun()

    def get_evidence_list(self, ctx: TenantContext, run_id: UUID) -> list[Any]:
        return self.observations.get(run_id, [])


class MockRunService:
    def __init__(self, repo: MockRunRepo) -> None:
        self.run_repo = repo
        self.session = None

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return self.run_repo.append_event(ctx, run_id, event_type, actor, payload)

    def save_diagnosis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        diagnosis_data: dict[str, Any],
        status: str = "draft",
    ) -> Any:
        return self.run_repo.save_diagnosis(ctx, run_id, diagnosis_data, status)

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        run = self.run_repo.get_by_id(ctx, run_id)
        run.status = to_status
        run.version += 1
        event = self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="status_transition",
            actor=actor,
            payload={"from_status": "investigating", "to_status": to_status, **(payload or {})},
        )
        return run, event


# ---------------------------------------------------------------------------
# Sample Alert Fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        alert_id="alt_test_v2_001",
        fired_at=datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
        service="orders",
        rule_id="orders_db_pool_exhaustion",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=9.5,
        window_seconds=300,
        severity="CRITICAL",
        status="firing",
    )


# ---------------------------------------------------------------------------
# 1. Normal 3-Step Trajectory
# ---------------------------------------------------------------------------
def test_normal_3_step_trajectory(sample_alert: AlertEvent) -> None:
    """Tests normal 3-step investigation trajectory:
    Turn 1: plan_step -> call_tool -> record -> plan_step
    Turn 2: plan_step -> update_ledger -> plan_step
    Turn 3: plan_step -> conclude -> finalize -> END
    """
    repo = MockRunRepo()
    run_service = MockRunService(repo)

    step1 = FlatAgentStepV2(
        step="call_tool",
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        reason="Querying database connection pool metrics for orders service",
    )

    step2 = FlatAgentStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "add",
                "statement": "Transient latency spike returned to baseline",
                "category": "false_alarm",
                "mechanism": "none",
                "service": None,
                "confidence_label": "high",
            },
            {
                "op": "link_evidence",
                "hypothesis_id": "hyp_01",
                "evidence_id": "ev_metric_01",
                "relation": "supports",
            },
            {
                "op": "set_status",
                "hypothesis_id": "hyp_01",
                "status": "supported",
            },
        ],
        reason="Formulating and grounding false alarm hypothesis using collected metrics",
    )

    diagnosis = Diagnosis(
        root_cause_service=None,
        root_cause_category="false_alarm",
        mechanism="none",
        culprit_deploy_id=None,
        evidence_ids=["ev_metric_01"],
        confidence=0.85,
    )

    step3 = FlatAgentStepV2(
        step="conclude",
        diagnosis_draft=diagnosis,
        leading_hypothesis_id="hyp_01",
        reason="Established false alarm supported by baseline return metric evidence",
    )

    llm_client = FakeLLMClient(responses=[step1, step2, step3])

    # Configure a mock tool registry that returns ev_metric_01
    registry = ToolRegistry()

    async def mock_get_metrics(*args: Any, **kwargs: Any) -> ToolSuccess:
        return ToolSuccess(
            tool="get_metrics",
            data={"points": [[1732449600, 0.45]], "returned_to_baseline": True, "metric_value": 0.45, "baseline": 0.50},
            evidence_id="ev_metric_01",
            captured_at=datetime.now(timezone.utc),
            window=Window(
                start=datetime(2026, 11, 24, 11, 55, 0, tzinfo=timezone.utc),
                end=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
            ),
            source="prometheus",
        )

    registry.register("get_metrics", GetMetricsInput, mock_get_metrics)

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=registry,
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )

    run_id = str(uuid4())
    tenant_id = str(uuid4())
    initial_state = create_initial_state(
        run_id=run_id,
        tenant_id=tenant_id,
        incident_id=str(uuid4()),
        alert=sample_alert,
        schema_version="v2",
    )

    final_state = run_investigation_graph(graph, initial_state)

    # Verifications
    assert final_state["status"] == "completed"  # false_alarm finalized with no_action
    assert final_state["step_count"] == 3
    assert final_state["ledger_update_count"] == 1
    assert "ev_metric_01" in final_state["evidence_ids"]

    # Ledger state verification
    ledger = final_state.get("hypotheses_ledger", {})
    assert "hyp_01" in ledger
    assert ledger["hyp_01"]["status"] == "supported"
    assert "ev_metric_01" in ledger["hyp_01"]["supporting_evidence_ids"]

    # Event emissions verification
    event_types = [e["event_type"] for e in repo.events]
    assert "tool_call" in event_types
    assert "hypothesis_added" in event_types
    assert "evidence_linked" in event_types
    assert "status_changed" in event_types
    assert "agent_step" in event_types


# ---------------------------------------------------------------------------
# 2. Ledger Op Rejected Then Repaired
# ---------------------------------------------------------------------------
def test_ledger_op_rejected_then_repaired(sample_alert: AlertEvent) -> None:
    """Verifies that an invalid hypothesis ledger operation triggers a typed StepRejection,
    feeds the repair hint back to the model, and recovers on the repair attempt.
    """
    repo = MockRunRepo()
    run_service = MockRunService(repo)

    # Initial response proposes linking a fabricated evidence ID (rejected by StepValidator)
    invalid_step = FlatAgentStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "add",
                "statement": "Connection pool exhaustion in orders service",
                "category": "bad_deploy",
                "mechanism": "connection_pool_exhaustion",
                "service": "orders",
            },
            {
                "op": "link_evidence",
                "hypothesis_id": "hyp_01",
                "evidence_id": "ev_fabricated_9999",  # NOT in run state!
                "relation": "supports",
            },
        ],
        reason="Proposing pool exhaustion with unobserved evidence",
    )

    # Repaired step: omits the fabricated link op
    repaired_step = FlatAgentStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "add",
                "statement": "Connection pool exhaustion in orders service",
                "category": "bad_deploy",
                "mechanism": "connection_pool_exhaustion",
                "service": "orders",
            },
        ],
        reason="Corrected hypothesis without ungrounded evidence citation",
    )

    conclude_step = FlatAgentStepV2(
        step="conclude",
        diagnosis_draft=Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=[],
            confidence=0.0,
        ),
        leading_hypothesis_id=None,
        reason="Insufficient evidence; concluding unknown",
    )

    llm_client = FakeLLMClient(responses=[invalid_step, repaired_step, conclude_step])

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )

    initial_state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        schema_version="v2",
    )

    final_state = run_investigation_graph(graph, initial_state)

    # Repair should have succeeded: ledger contains hyp_01
    assert "hyp_01" in final_state.get("hypotheses_ledger", {})
    # invalid_step_count was NOT incremented because repair succeeded
    assert final_state.get("invalid_step_count", 0) == 0


# ---------------------------------------------------------------------------
# 3. Step Cap Termination (Budget Interaction)
# ---------------------------------------------------------------------------
def test_step_cap_termination(sample_alert: AlertEvent) -> None:
    """Verifies that update_hypotheses steps do NOT consume tool call budget (remaining_calls)
    but DO count toward the step cap (MAX_STEPS = 2 * MAX_TOOL_CALLS + 4), triggering
    clean termination when reached.
    """
    repo = MockRunRepo()
    run_service = MockRunService(repo)

    # Provide a recurring update_hypotheses response
    repeated_step = FlatAgentStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "add",
                "statement": "Potential latency degradation in inventory service",
                "category": "bad_deploy",
                "mechanism": "cache_bypass_latency",
                "service": "inventory",
            }
        ],
        reason="Refining hypothesis incrementally",
    )

    # Create 10 scripted turns
    llm_client = FakeLLMClient(responses=[repeated_step] * 10)

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )

    # Configure a low max_steps (e.g. 4)
    initial_state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        max_calls=4,
        schema_version="v2",
    )
    initial_state["max_steps"] = 4  # Cap at 4 turns

    final_state = run_investigation_graph(graph, initial_state)

    # Assert termination by step cap
    assert final_state["termination_reason"] == TerminationReason.STEP_CAP_REACHED.value
    # remaining_calls was NOT decremented by update_hypotheses
    assert final_state["remaining_calls"] == 4
    # step count reached max_steps
    assert final_state["step_count"] >= 4


# ---------------------------------------------------------------------------
# 4. Compaction Keeps Cited Evidence IDs
# ---------------------------------------------------------------------------
def test_compaction_keeps_cited_evidence_ids(sample_alert: AlertEvent) -> None:
    """Verifies that per-turn context assembly NEVER drops or compacts evidence IDs
    cited in the hypothesis ledger, even under heavy token load.
    """
    state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        schema_version="v2",
    )

    # Create 8 evidence records and tool trace entries
    evidence_ids: list[str] = []
    tool_trace: list[dict[str, Any]] = []

    for i in range(8):
        ev_id = f"ev_tool_obs_{i:04d}"
        evidence_ids.append(ev_id)
        ref = ToolCallRecordRef(
            tool="get_metrics",
            args_summary=f"service=orders, metric=test_{i}, details={'x'*150}",
            ok=True,
            evidence_id=ev_id,
        )
        tool_trace.append(ref.model_dump())

    # State: ev_tool_obs_0000 (oldest) is cited by hyp_01
    state["evidence_ids"] = evidence_ids
    state["tool_trace"] = tool_trace
    state["hypotheses_ledger"] = {
        "hyp_01": {
            "id": "hyp_01",
            "statement": "Database connection pool exhaustion in orders service",
            "category": "bad_deploy",
            "mechanism": "connection_pool_exhaustion",
            "service": "orders",
            "status": "supported",
            "supporting_evidence_ids": ["ev_tool_obs_0000"],  # Cited oldest ID
            "contrary_evidence_ids": [],
            "tests_run": [],
        }
    }

    # Assemble context with a tight token budget to force compaction
    assembled = build_investigator_context(
        state=state,
        max_prompt_tokens=1500,  # Tight ceiling to trigger compaction
    )

    # In user content, ev_tool_obs_0000 must NOT be marked [compacted]
    user_prompt = assembled.messages[1].content
    assert "ev_tool_obs_0000" in user_prompt

    # Non-cited older items (like ev_tool_obs_0001) should be compacted
    assert "[compacted]" in user_prompt
    assert "`ev_tool_obs_0000` (get_metrics): service=orders" in user_prompt


# ---------------------------------------------------------------------------
# 5. Router Refuses Conclude Before First Ledger Update
# ---------------------------------------------------------------------------
def test_router_refuses_conclude_before_first_ledger_update(sample_alert: AlertEvent) -> None:
    """Verifies that route_after_plan_step enforces the mandatory ledger step:
    if tool_trace has entries and ledger_update_count == 0, conclude is refused
    and routed back to plan_step. Once ledger_update_count > 0, conclude is allowed.
    """
    state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        schema_version="v2",
    )

    # Simulate tool dispatch completed
    state["tool_trace"] = [
        {"tool": "get_metrics", "ok": True, "evidence_id": "ev_01"}
    ]
    state["ledger_update_count"] = 0
    state["last_step_decision"] = {
        "step": "conclude",
        "reason": "Attempting premature conclusion",
    }

    # Router must REFUSE conclude and loop back to plan_step
    next_node = route_after_plan_step(state)
    assert next_node == "plan_step", "Router should refuse conclude before any ledger update"

    # Now simulate ledger updated at least once
    state["ledger_update_count"] = 1
    next_node_after_update = route_after_plan_step(state)
    assert next_node_after_update == "conclude", "Router should allow conclude after ledger update"


# ---------------------------------------------------------------------------
# 6. Worst-Case Token Cap Test (4 Hypotheses & 8 Calls)
# ---------------------------------------------------------------------------
def test_worst_case_token_cap_with_4_hypotheses_and_8_calls(sample_alert: AlertEvent) -> None:
    """Verifies that a maximal run with 4 hypotheses and 8 tool calls satisfies
    the 4,000 per-turn prompt token cap.
    """
    state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        max_calls=8,
        schema_version="v2",
    )

    # 4 distinct hypotheses
    ledger_records: dict[str, dict[str, Any]] = {
        "hyp_01": {
            "id": "hyp_01",
            "statement": "Connection pool exhausted on orders service caused by high concurrent checkouts",
            "category": "bad_deploy",
            "mechanism": "connection_pool_exhaustion",
            "service": "orders",
            "status": "supported",
            "supporting_evidence_ids": ["ev_m01", "ev_t01"],
            "contrary_evidence_ids": [],
            "tests_run": [
                {"evidence_id": "ev_m01", "tool": "get_metrics", "args_hash": "a1b2c3d4"}
            ],
        },
        "hyp_02": {
            "id": "hyp_02",
            "statement": "Redis cache dependency timeout on inventory service causing cascade delays",
            "category": "dependency_failure",
            "mechanism": "downstream_timeout",
            "service": "inventory",
            "status": "refuted",
            "supporting_evidence_ids": [],
            "contrary_evidence_ids": ["ev_l02"],
            "tests_run": [
                {"evidence_id": "ev_l02", "tool": "search_logs", "args_hash": "e5f6g7h8"}
            ],
        },
        "hyp_03": {
            "id": "hyp_03",
            "statement": "Cache bypass causing latency spikes on inventory service during traffic surge",
            "category": "bad_deploy",
            "mechanism": "cache_bypass_latency",
            "service": "inventory",
            "status": "inconclusive",
            "supporting_evidence_ids": ["ev_m03"],
            "contrary_evidence_ids": [],
            "tests_run": [],
        },
        "hyp_04": {
            "id": "hyp_04",
            "statement": "Decoy deployment artifact orders_v110 misidentified as root cause mechanism",
            "category": "bad_deploy",
            "mechanism": "none",
            "service": "orders",
            "status": "refuted",
            "supporting_evidence_ids": [],
            "contrary_evidence_ids": ["ev_d04"],
            "tests_run": [],
        },
    }

    # 8 tool calls with evidence IDs and realistic args
    evidence_ids: list[str] = [
        "ev_m01", "ev_t01", "ev_l02", "ev_m03", "ev_d04", "ev_m05", "ev_l06", "ev_t07"
    ]
    tool_trace: list[dict[str, Any]] = []
    for ev in evidence_ids:
        tool_trace.append(
            ToolCallRecordRef(
                tool="get_metrics",
                args_summary=f"service=orders, metric=db_pool, evidence={ev}, window=300s",
                ok=True,
                evidence_id=ev,
            ).model_dump()
        )

    state["evidence_ids"] = evidence_ids
    state["tool_trace"] = tool_trace
    state["hypotheses_ledger"] = ledger_records
    state["messages_summary"] = (
        "Turn 1: call_tool(get_metrics) - Checked pool.\n"
        "Turn 2: update_hypotheses(2 ops) - Added hyp_01, hyp_02.\n"
        "Turn 3: call_tool(search_logs) - Verified Redis timeout.\n"
        "Turn 4: update_hypotheses(1 ops) - Refuted hyp_02.\n"
        "Turn 5: call_tool(get_trace) - Traced order checkout cascade.\n"
    )

    assembled = build_investigator_context(
        state=state,
        max_prompt_tokens=DEFAULT_MAX_PROMPT_TOKENS,
    )

    # Assert under the 4,000 token limit
    assert assembled.total_tokens_est <= DEFAULT_MAX_PROMPT_TOKENS
    # Assert ledger digest is visible in prompt
    assert "### Hypothesis Ledger (4 items)" in assembled.messages[1].content
    assert "`hyp_01` [supported]" in assembled.messages[1].content
    assert "`hyp_02` [refuted]" in assembled.messages[1].content


# ---------------------------------------------------------------------------
# 7. Interleaved Events Acceptance Generator
# ---------------------------------------------------------------------------
def test_interleaved_events_table(sample_alert: AlertEvent) -> None:
    """Executes a complete scripted trajectory and generates the interleaved
    run_events audit table required for Feature 6 acceptance.
    """
    repo = MockRunRepo()
    run_service = MockRunService(repo)

    step1 = FlatAgentStepV2(
        step="call_tool",
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        tests_hypothesis_id=None,
        reason="Querying database connection pool metrics for orders service",
    )

    step2 = FlatAgentStepV2(
        step="update_hypotheses",
        operations=[
            {
                "op": "add",
                "statement": "Transient latency spike returned to baseline",
                "category": "false_alarm",
                "mechanism": "none",
                "service": None,
                "confidence_label": "high",
            },
            {
                "op": "link_evidence",
                "hypothesis_id": "hyp_01",
                "evidence_id": "ev_metric_orders_01",
                "relation": "supports",
            },
            {
                "op": "set_status",
                "hypothesis_id": "hyp_01",
                "status": "supported",
            },
        ],
        reason="Formulating and grounding false alarm hypothesis",
    )

    diagnosis = Diagnosis(
        root_cause_service=None,
        root_cause_category="false_alarm",
        mechanism="none",
        culprit_deploy_id=None,
        evidence_ids=["ev_metric_orders_01"],
        confidence=0.90,
    )

    step3 = FlatAgentStepV2(
        step="conclude",
        diagnosis_draft=diagnosis,
        leading_hypothesis_id="hyp_01",
        reason="False alarm confirmed by baseline return telemetry",
    )

    llm_client = FakeLLMClient(responses=[step1, step2, step3])

    registry = ToolRegistry()

    async def mock_tool(*args: Any, **kwargs: Any) -> ToolSuccess:
        return ToolSuccess(
            tool="get_metrics",
            data={"points": [[1732449600, 0.45]], "returned_to_baseline": True, "metric_value": 0.45, "baseline": 0.50},
            evidence_id="ev_metric_orders_01",
            captured_at=datetime.now(timezone.utc),
            window=Window(
                start=datetime(2026, 11, 24, 11, 55, 0, tzinfo=timezone.utc),
                end=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
            ),
            source="prometheus",
        )

    registry.register("get_metrics", GetMetricsInput, mock_tool)

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=registry,
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )

    initial_state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert=sample_alert,
        schema_version="v2",
    )

    final_state = run_investigation_graph(graph, initial_state)
    assert final_state["status"] == "completed"

    # Print markdown table for acceptance paste
    print("\n\n=== SCRIPTED RUN INTERLEAVED EVENTS TABLE ===")
    print("| Seq | Event Type | Actor | Summary / Key Payload |")
    print("| :--- | :--- | :--- | :--- |")
    for ev in repo.events:
        etype = ev["event_type"]
        actor = ev["actor"]
        seq = ev["seq"]
        p = ev.get("payload", {})
        if etype == "agent_step":
            summary = f"kind={p.get('step_kind')}, tool={p.get('tool')}, reason={p.get('reason')}"
        elif etype == "tool_call":
            summary = f"tool={p.get('tool')}, ok={p.get('ok')}, ev_id={p.get('evidence_id')}"
        elif etype == "hypothesis_added":
            summary = f"id={p.get('hypothesis_id')}, statement='{p.get('statement')}'"
        elif etype == "evidence_linked":
            summary = f"{p.get('evidence_id')} ({p.get('relation')}) -> {p.get('hypothesis_id')}"
        elif etype == "status_changed":
            summary = f"{p.get('hypothesis_id')} status -> {p.get('new_status')}"
        else:
            summary = json.dumps(p)[:80]
        print(f"| {seq} | `{etype}` | {actor} | {summary} |")
    print("=============================================\n")
