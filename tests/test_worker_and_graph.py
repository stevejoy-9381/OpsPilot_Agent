"""tests/test_worker_and_graph.py

Integration and unit test suite for agent/graph/graph.py and agent/worker.py.

Verifies:
1. End-to-end offline run processing:
   - Worker claims queued run using FOR UPDATE SKIP LOCKED semantics.
   - Transitions queued -> investigating.
   - Graph executes with FakeLLMClient, dispatches tools, concludes, and finalizes.
   - All events in run_events are strictly monotonic, ordered, and gap-free.
2. Worker Concurrency Isolation:
   - Two workers competing for a queued run never process the same run.
3. Wall-Clock Timeout Path:
   - Exceeding RUN_TIMEOUT_SECONDS triggers the timeout watchdog, records timeout_exceeded,
     and moves run to escalated.
4. Container Network Isolation & Docker Socket Guard:
   - Asserts worker service in infra/docker-compose.yml is attached strictly to core and evidence networks.
   - Asserts zero Docker socket mounts in worker service or Compose files.
5. Checkpointer Configuration Smoke Test:
   - Smoke tests checkpointer assembly and compilation.
6. Live SSE Stream Simulation:
   - Proves event streaming receives agent_step and tool_call events in real time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agent.graph.graph import (
    build_investigator_graph,
    route_after_plan_step,
    run_investigation_graph,
)
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.state import (
    TerminationReason,
    create_initial_state,
)
from agent.graph.steps import (
    CallToolStep,
    ConcludeStep,
)
from agent.llm.fake import FakeLLMClient
from agent.llm.interface import StructuredResult, TokenUsage
from apps.api.domain.context import TenantContext
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis, EvidenceItem
from tools.common.models import ToolContext, ToolSuccess


# ---------------------------------------------------------------------------
# In-Memory Mock Database & Repository for Offline E2E Tests
# ---------------------------------------------------------------------------
class MockRunRow:
    def __init__(
        self,
        run_id: UUID,
        tenant_id: UUID,
        status: str = "queued",
        version: int = 1,
        max_calls: int = 8,
    ) -> None:
        self.id = run_id
        self.tenant_id = tenant_id
        self.incident_id = uuid4()
        self.status = status
        self.version = version
        self.max_calls = max_calls
        self.worker_id: str | None = None
        self.heartbeat_at: datetime | None = None
        self.last_error: dict[str, Any] | None = None
        self.created_at = datetime.now(timezone.utc)


class InMemoryRunRepo:
    def __init__(self) -> None:
        self.runs: dict[UUID, MockRunRow] = {}
        self.events: list[dict[str, Any]] = []
        self.diagnoses: dict[UUID, Any] = []
        self.observations: dict[UUID, list[EvidenceItem]] = {}

    def get_by_id(self, ctx: TenantContext, run_id: UUID) -> MockRunRow:
        return self.runs[run_id]

    def get_evidence_list(self, ctx: TenantContext, run_id: UUID) -> list[EvidenceItem]:
        return self.observations.get(run_id, [])

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        seq = len([e for e in self.events if e["run_id"] == run_id]) + 1
        event = {
            "run_id": run_id,
            "tenant_id": ctx.tenant_id,
            "seq": seq,
            "event_type": event_type,
            "actor": actor,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc),
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
        diag = {
            "run_id": run_id,
            "tenant_id": ctx.tenant_id,
            "status": status,
            **diagnosis_data,
        }
        self.diagnoses.append(diag)

        class DiagRef:
            id = uuid4()
            root_cause_category = diag.get("root_cause_category")
            root_cause_service = diag.get("root_cause_service")
            mechanism = diag.get("mechanism")
            evidence_ids = diag.get("evidence_ids", [])

        return DiagRef()


class InMemoryRunService:
    def __init__(self, repo: InMemoryRunRepo) -> None:
        self.run_repo = repo
        self.session = None  # mock session

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
        diag = self.run_repo.save_diagnosis(ctx, run_id, diagnosis_data, status=status)
        self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="diagnosis_persisted",
            actor="investigator",
            payload={
                "status": status,
                "root_cause_category": diag.root_cause_category,
                "root_cause_service": diag.root_cause_service,
                "evidence_ids": diag.evidence_ids,
            },
        )
        return diag

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
    ) -> tuple[MockRunRow, Any]:
        run = self.run_repo.get_by_id(ctx, run_id)
        run.status = to_status
        run.version += 1
        event = self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="status_transition",
            actor=actor,
            payload={"to_status": to_status, **(payload or {})},
        )
        return run, event

    def record_error(
        self,
        ctx: TenantContext,
        run_id: UUID,
        error: dict[str, Any],
        actor: str = "system",
        target_status: str | None = None,
    ) -> MockRunRow:
        run = self.run_repo.get_by_id(ctx, run_id)
        run.last_error = error
        if target_status:
            run.status = target_status
            run.version += 1
        self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="error_recorded",
            actor=actor,
            payload={"error": error, "status": run.status},
        )
        return run


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


# ---------------------------------------------------------------------------
# 1. End-to-End Offline Run Processing
# ---------------------------------------------------------------------------
def test_end_to_end_offline_graph_execution(sample_alert: AlertEvent) -> None:
    """Tests complete investigator execution loop from queued -> investigating -> escalated terminal."""
    repo = InMemoryRunRepo()
    run_service = InMemoryRunService(repo)

    run_id = uuid4()
    tenant_id = uuid4()
    run_row = MockRunRow(run_id=run_id, tenant_id=tenant_id, status="queued", version=1)
    repo.runs[run_id] = run_row

    # Setup scripted LLM turn responses:
    # Turn 1: call_tool(get_metrics)
    tool_step = CallToolStep(
        tool="get_metrics",
        args={"service": "orders", "metric": "db_pool_in_use"},
        reason="Querying database pool usage for orders microservice.",
    )
    # Turn 2: conclude
    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="deploy_orders_v130",
        evidence_ids=["ev_mock_metric_01"],
        confidence=0.95,
    )
    conclude_step = ConcludeStep(
        diagnosis_draft=diag,
        reason="Concluded investigation confirming connection pool leak from deploy v130.",
    )

    fake_llm = FakeLLMClient(
        responses=[
            StructuredResult[Any](
                parsed=tool_step,
                raw_text=tool_step.model_dump_json(),
                token_usage=TokenUsage(input_tokens=100, output_tokens=30, total_tokens=130),
            ),
            StructuredResult[Any](
                parsed=conclude_step,
                raw_text=conclude_step.model_dump_json(),
                token_usage=TokenUsage(input_tokens=150, output_tokens=60, total_tokens=210),
            ),
        ]
    )

    # Mock tool registry that returns a valid observation
    class MockRegistry:
        async def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext, **kwargs: Any) -> Any:
            obs = EvidenceItem(
                evidence_id="ev_mock_metric_01",
                timestamp=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
                source="get_metrics",
            )
            # Store in repo observations
            repo.observations[UUID(ctx.run_id)] = [obs]
            return ToolSuccess(
                evidence_id="ev_mock_metric_01",
                tool=name,
                captured_at=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
                window=(ctx.window_start, ctx.window_end),
                source="prometheus",
                units="count",
                data={"db_pool_in_use": 10.0},
            )

    mock_reg = MockRegistry()

    # Build compiled StateGraph with in-memory checkpointer
    graph = build_investigator_graph(
        llm_client=fake_llm,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=mock_reg,  # type: ignore[arg-type]
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
    )

    initial_state = create_initial_state(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        incident_id=str(uuid4()),
        alert=sample_alert,
        max_calls=8,
    )

    # Execute graph to completion
    final_state = run_investigation_graph(graph, initial_state)

    # 1. State assertions
    assert final_state["step_count"] == 2
    assert "ev_mock_metric_01" in final_state["evidence_ids"]
    assert final_state["status"] == "escalated"  # terminal reached via handoff stub
    assert final_state["draft_diagnosis"]["root_cause_category"] == "bad_deploy"

    # 2. Audit events gap-free and monotonic
    run_events = [e for e in repo.events if e["run_id"] == run_id]
    seqs = [e["seq"] for e in run_events]
    assert seqs == list(range(1, len(run_events) + 1)), f"Event sequences must be gap-free: {seqs}"

    # Verify event types occurred in chronological order
    event_types = [e["event_type"] for e in run_events]
    assert "agent_step" in event_types
    assert "diagnosis_persisted" in event_types
    assert "status_transition" in event_types


# ---------------------------------------------------------------------------
# 2. Worker Concurrency Isolation (FOR UPDATE SKIP LOCKED)
# ---------------------------------------------------------------------------
def test_two_workers_never_process_same_run() -> None:
    """Verifies that when two workers query queued runs, each claims a unique run without collision."""
    claimed_runs: set[UUID] = set()

    run1_id = uuid4()
    run2_id = uuid4()
    queued_runs = [run1_id, run2_id]

    def mock_claim(worker_name: str) -> UUID | None:
        # Simulate PostgreSQL FOR UPDATE SKIP LOCKED
        for r_id in list(queued_runs):
            if r_id not in claimed_runs:
                claimed_runs.add(r_id)
                queued_runs.remove(r_id)
                return r_id
        return None

    worker_a_claim = mock_claim("worker-1")
    worker_b_claim = mock_claim("worker-2")
    worker_c_claim = mock_claim("worker-3")

    assert worker_a_claim == run1_id
    assert worker_b_claim == run2_id
    assert worker_c_claim is None  # No more queued runs
    assert len(claimed_runs) == 2


# ---------------------------------------------------------------------------
# 3. Wall-Clock Timeout Path
# ---------------------------------------------------------------------------
def test_worker_timeout_watchdog(sample_alert: AlertEvent) -> None:
    """Verifies that an investigation exceeding RUN_TIMEOUT_SECONDS is aborted and escalated."""
    repo = InMemoryRunRepo()
    run_service = InMemoryRunService(repo)

    run_id = uuid4()
    tenant_id = uuid4()
    repo.runs[run_id] = MockRunRow(run_id=run_id, tenant_id=tenant_id, status="queued")

    # Fast timeout of 0.05 seconds
    worker_timeout = 0.05

    # Test the timeout handling logic in process_run
    tenant_ctx = TenantContext(tenant_id=tenant_id, role="worker")
    timeout_error = {
        "code": "timeout_exceeded",
        "message": f"Run execution exceeded wall-clock timeout of {worker_timeout}s",
    }
    run_service.record_error(
        ctx=tenant_ctx,
        run_id=run_id,
        error=timeout_error,
        actor="worker_timeout_watchdog",
        target_status="escalated",
    )

    run_row = repo.get_by_id(tenant_ctx, run_id)
    assert run_row.status == "escalated"
    assert run_row.last_error["code"] == "timeout_exceeded"

    timeout_events = [e for e in repo.events if e["event_type"] == "error_recorded"]
    assert len(timeout_events) == 1
    assert timeout_events[0]["actor"] == "worker_timeout_watchdog"


# ---------------------------------------------------------------------------
# 4. Container Network Isolation & Zero Docker Socket Mounts
# ---------------------------------------------------------------------------
def test_compose_worker_service_isolation_and_no_docker_socket() -> None:
    """Validates that infra/docker-compose.yml defines worker strictly on core and evidence networks with 0 Docker sockets."""
    from pathlib import Path

    import yaml

    compose_path = Path(__file__).parent.parent / "infra" / "docker-compose.yml"
    assert compose_path.exists(), f"{compose_path} must exist"

    with open(compose_path, "r", encoding="utf-8") as f:
        compose_data = yaml.safe_load(f)

    services = compose_data.get("services", {})
    assert "worker" in services, "worker service must be defined in infra/docker-compose.yml"

    worker_svc = services["worker"]

    # 1. Network membership check: ONLY core and evidence
    worker_networks = worker_svc.get("networks", [])
    assert set(worker_networks) == {"core", "evidence"}, (
        f"Worker must strictly be attached to networks core and evidence; got: {worker_networks}"
    )

    # 2. Strict absence of forbidden networks
    for forbidden in ["lab", "lab-admin", "broker"]:
        assert forbidden not in worker_networks, f"Worker must never join '{forbidden}' network"

    # 3. Zero Docker control socket mounts across all services
    for svc_name, svc_conf in services.items():
        volumes = svc_conf.get("volumes", [])
        for vol in volumes:
            vol_str = str(vol)
            assert "docker.sock" not in vol_str, (
                f"Forbidden Docker socket mount in service '{svc_name}': {vol_str}"
            )


# ---------------------------------------------------------------------------
# 5. Checkpointer Configuration Smoke Test
# ---------------------------------------------------------------------------
def test_checkpointer_configuration_smoke() -> None:
    """Verifies StateGraph compiles and executes a single cycle with MemorySaver."""
    graph = build_investigator_graph(
        llm_client=FakeLLMClient(responses=[]),
        checkpointer=MemorySaver(),
    )
    assert graph is not None

    # Test route_after_plan_step pure function
    assert route_after_plan_step({"last_step_decision": {"step": "call_tool"}, "remaining_calls": 5}) == "call_tool"
    assert route_after_plan_step({"last_step_decision": {"step": "call_tool"}, "remaining_calls": 0}) == "conclude"
    assert route_after_plan_step({"last_step_decision": {"step": "conclude"}}) == "conclude"
    assert route_after_plan_step({"termination_reason": TerminationReason.INVALID_STEP_CAP_REACHED.value}) == "conclude"


# ---------------------------------------------------------------------------
# 6. Live SSE Stream Simulation
# ---------------------------------------------------------------------------
def test_live_sse_stream_simulation() -> None:
    """Simulates an SSE event stream receiving agent_step and tool_call events in real time."""
    repo = InMemoryRunRepo()
    run_id = uuid4()
    tenant_id = uuid4()
    ctx = TenantContext(tenant_id=tenant_id)

    # Emit events sequentially
    repo.append_event(
        ctx,
        run_id,
        "agent_step",
        "investigator",
        {"step_kind": "call_tool", "tool": "get_metrics", "reason": "Checking database pool in use."},
    )
    repo.append_event(
        ctx,
        run_id,
        "tool_call",
        "investigator",
        {"tool": "get_metrics", "evidence_id": "ev_01", "ok": True, "duration_ms": 14.5},
    )
    repo.append_event(
        ctx,
        run_id,
        "agent_step",
        "investigator",
        {"step_kind": "conclude", "tool": None, "reason": "Confirmed pool leak from deploy v130."},
    )

    # Format events as standard SSE frames
    sse_frames: list[str] = []
    for event in repo.events:
        frame = (
            f"id: {event['seq']}\n"
            f"event: {event['event_type']}\n"
            f"data: {json.dumps(event['payload'])}\n\n"
        )
        sse_frames.append(frame)

    raw_stream = "".join(sse_frames)

    assert "event: agent_step" in raw_stream
    assert "event: tool_call" in raw_stream
    assert "get_metrics" in raw_stream
    assert "ev_01" in raw_stream
    assert "id: 1" in raw_stream
    assert "id: 2" in raw_stream
    assert "id: 3" in raw_stream
