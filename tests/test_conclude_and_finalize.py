"""tests/test_conclude_and_finalize.py

Unit tests for agent/graph/nodes/conclude.py and finalize.py.

Verifies:
1. conclude_node:
   - Validates model's ConcludeStep or draft_diagnosis.
   - Falls back safely to deterministic unknown Diagnosis on missing/invalid input.
2. Fabricated evidence ID stripped and downgrade works:
   - Partial fabrication: Fabricated citation stripped, valid citations retained, claim maintained.
   - Complete fabrication: Zero valid citations remain for non-unknown diagnosis => downgraded
     to unknown with reason "unsupported_by_recorded_evidence".
3. Out-of-window evidence rejected:
   - Evidence outside the incident timeline window is flagged and stripped.
4. Valid diagnosis persisted:
   - Persists diagnosis draft in app.diagnoses with status="draft".
   - Emits diagnosis_persisted event.
5. Handoff stub reaches terminal state:
   - DefaultHandoffStub transitions run to "escalated" under reason "investigation_complete_no_planner".
6. Optimistic locking conflict handled:
   - ConcurrentModificationError caught and recorded in last_error without crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent.graph.nodes.conclude import conclude_node
from agent.graph.nodes.finalize import finalize_node
from agent.graph.state import (
    InvestigationState,
    create_initial_state,
)
from agent.graph.steps import ConcludeStep
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import ConcurrentModificationError
from baseline.alerts.models import AlertEvent
from evals.graders.models import Diagnosis, EvidenceItem


# ---------------------------------------------------------------------------
# In-Memory Mock RunService
# ---------------------------------------------------------------------------
class MockRunRecord:
    def __init__(self, run_id: UUID, tenant_id: UUID, version: int = 1, status: str = "investigating"):
        self.id = run_id
        self.tenant_id = tenant_id
        self.version = version
        self.status = status


class MockDiagnosisRecord:
    def __init__(self, **kwargs: Any):
        self.id = uuid4()
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockRunRepo:
    def __init__(self) -> None:
        self.runs: dict[UUID, MockRunRecord] = {}
        self.diagnoses: dict[UUID, MockDiagnosisRecord] = {}
        self.observations: dict[UUID, list[EvidenceItem]] = {}
        self.events: list[dict[str, Any]] = []

    def get_by_id(self, ctx: TenantContext, run_id: UUID) -> MockRunRecord:
        if run_id not in self.runs:
            self.runs[run_id] = MockRunRecord(run_id=run_id, tenant_id=ctx.tenant_id)
        return self.runs[run_id]

    def get_evidence_list(self, ctx: TenantContext, run_id: UUID) -> list[EvidenceItem]:
        return self.observations.get(run_id, [])

    def get_run_diagnosis(self, ctx: TenantContext, run_id: UUID) -> MockDiagnosisRecord | None:
        return self.diagnoses.get(run_id)

    def save_diagnosis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        diagnosis_data: dict[str, Any],
        status: str = "draft",
    ) -> MockDiagnosisRecord:
        diag = MockDiagnosisRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            status=status,
            **diagnosis_data,
        )
        self.diagnoses[run_id] = diag
        return diag

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        event = {
            "run_id": str(run_id),
            "tenant_id": str(ctx.tenant_id),
            "event_type": event_type,
            "actor": actor,
            "payload": payload or {},
            "seq": len(self.events) + 1,
        }
        self.events.append(event)
        # Return a simple mock object with seq
        class EventRef:
            seq = event["seq"]
        return EventRef()


class MockRunService:
    def __init__(self, repo: MockRunRepo | None = None) -> None:
        self.run_repo = repo or MockRunRepo()
        self.transition_calls: list[dict[str, Any]] = []

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
    ) -> MockDiagnosisRecord:
        diag = self.run_repo.save_diagnosis(ctx, run_id, diagnosis_data, status=status)
        self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="diagnosis_persisted",
            actor="investigator",
            payload={
                "diagnosis_id": str(diag.id),
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
    ) -> tuple[MockRunRecord, Any]:
        run = self.run_repo.get_by_id(ctx, run_id)
        if run.version != expected_version:
            raise ConcurrentModificationError(
                entity_id=str(run_id),
                expected_version=expected_version,
                actual_version=run.version,
            )
        run.status = to_status
        run.version += 1
        event = self.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="status_transition",
            actor=actor,
            payload={"to_status": to_status, **(payload or {})},
        )
        self.transition_calls.append({"to_status": to_status, "actor": actor, "version": run.version})
        return run, event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        alert_id="alt_orders_pool_001",
        fired_at=datetime(2026, 11, 24, 12, 10, 0, tzinfo=timezone.utc),
        service="orders",
        rule_id="rule_db_pool_exhaustion",
        metric="db_pool_in_use",
        threshold=8.0,
        observed_value=10.0,
        window_seconds=600,
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
# 1. conclude_node Tests
# ---------------------------------------------------------------------------
def test_conclude_node_validates_draft(base_state: InvestigationState) -> None:
    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_01", "ev_02"],
        confidence=0.9,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    updates = conclude_node(base_state)
    assert updates["status"] == "concluded_draft"
    assert updates["draft_diagnosis"]["root_cause_category"] == "bad_deploy"
    assert updates["draft_diagnosis"]["evidence_ids"] == ["ev_01", "ev_02"]


def test_conclude_node_extracts_from_last_step(base_state: InvestigationState) -> None:
    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        evidence_ids=["ev_01"],
        confidence=0.85,
    )
    step = ConcludeStep(
        diagnosis_draft=diag,
        reason="Concluding with confirmed connection pool leak.",
    )
    base_state["last_step_decision"] = step.model_dump()
    base_state["draft_diagnosis"] = None

    updates = conclude_node(base_state)
    assert updates["status"] == "concluded_draft"
    assert updates["draft_diagnosis"]["root_cause_category"] == "bad_deploy"


def test_conclude_node_fallback_on_missing(base_state: InvestigationState) -> None:
    base_state["draft_diagnosis"] = None
    base_state["last_step_decision"] = None

    updates = conclude_node(base_state)
    assert updates["status"] == "concluded_draft"
    assert updates["draft_diagnosis"]["root_cause_category"] == "unknown"
    assert updates["draft_diagnosis"]["confidence"] == 0.0


# ---------------------------------------------------------------------------
# 2. Fabricated Evidence ID Stripped & Downgrade Works
# ---------------------------------------------------------------------------
def test_partial_fabricated_evidence_stripped(base_state: InvestigationState) -> None:
    """When some citations are fabricated, strip them but keep valid ones and category."""
    run_id = UUID(base_state["run_id"])
    run_service = MockRunService()

    # Record 1 real observation
    valid_obs = EvidenceItem(
        evidence_id="ev_metric_valid_01",
        timestamp=datetime(2026, 11, 24, 12, 8, 0, tzinfo=timezone.utc),
        source="get_metrics",
    )
    run_service.run_repo.observations[run_id] = [valid_obs]

    # Diagnosis cites 1 valid ID and 1 hallucinated ID
    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_metric_valid_01", "ev_hallucinated_999"],
        confidence=0.92,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    incident_window = (
        datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 24, 12, 20, 0, tzinfo=timezone.utc),
    )

    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        incident_window=incident_window,
    )

    # Fabricated ID stripped
    assert updates["draft_diagnosis"]["evidence_ids"] == ["ev_metric_valid_01"]
    # Category retained since 1 valid ID remains
    assert updates["draft_diagnosis"]["root_cause_category"] == "bad_deploy"

    # Citation rejected event emitted
    rejected_events = [e for e in run_service.run_repo.events if e["event_type"] == "citation_rejected"]
    assert len(rejected_events) == 1
    assert "ev_hallucinated_999" in rejected_events[0]["payload"]["rejected_ids"]


def test_complete_fabricated_evidence_downgrades_to_unknown(base_state: InvestigationState) -> None:
    """When ALL citations are fabricated, diagnosis must downgrade to unknown."""
    run_service = MockRunService()

    # Empty observations: model fabricated everything!
    run_id = UUID(base_state["run_id"])
    run_service.run_repo.observations[run_id] = []

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_fake_01", "ev_fake_02"],
        confidence=0.95,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    incident_window = (
        datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 24, 12, 20, 0, tzinfo=timezone.utc),
    )

    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        incident_window=incident_window,
    )

    # Evidence IDs cleared
    assert updates["draft_diagnosis"]["evidence_ids"] == []
    # Downgraded to unknown
    assert updates["draft_diagnosis"]["root_cause_category"] == "unknown"
    assert updates["draft_diagnosis"]["mechanism"] == "unknown"
    assert updates["draft_diagnosis"]["root_cause_service"] is None
    assert updates["draft_diagnosis"]["confidence"] == 0.0

    # Events emitted
    event_types = [e["event_type"] for e in run_service.run_repo.events]
    assert "citation_rejected" in event_types
    assert "diagnosis_downgraded" in event_types

    downgrade_event = next(e for e in run_service.run_repo.events if e["event_type"] == "diagnosis_downgraded")
    assert downgrade_event["payload"]["reason"] == "unsupported_by_recorded_evidence"


# ---------------------------------------------------------------------------
# 3. Out-of-Window Evidence Rejected
# ---------------------------------------------------------------------------
def test_out_of_window_evidence_rejected(base_state: InvestigationState) -> None:
    run_service = MockRunService()
    run_id = UUID(base_state["run_id"])

    # Observation is 4 hours after incident window
    out_of_window_obs = EvidenceItem(
        evidence_id="ev_metric_stale",
        timestamp=datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc),
        source="get_metrics",
    )
    run_service.run_repo.observations[run_id] = [out_of_window_obs]

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        evidence_ids=["ev_metric_stale"],
        confidence=0.88,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    incident_window = (
        datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 24, 12, 20, 0, tzinfo=timezone.utc),
    )

    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        incident_window=incident_window,
    )

    # Stale evidence rejected and diagnosis downgraded because 0 valid remain
    assert updates["draft_diagnosis"]["evidence_ids"] == []
    assert updates["draft_diagnosis"]["root_cause_category"] == "unknown"

    rejected_event = next(e for e in run_service.run_repo.events if e["event_type"] == "citation_rejected")
    assert "ev_metric_stale" in rejected_event["payload"]["out_of_window_ids"]


# ---------------------------------------------------------------------------
# 4. Valid Diagnosis Persisted
# ---------------------------------------------------------------------------
def test_valid_diagnosis_persisted(base_state: InvestigationState) -> None:
    run_service = MockRunService()
    run_id = UUID(base_state["run_id"])

    # Both observations exist and are in window
    valid_obs_1 = EvidenceItem(
        evidence_id="ev_metric_01",
        timestamp=datetime(2026, 11, 24, 12, 5, 0, tzinfo=timezone.utc),
        source="get_metrics",
    )
    valid_obs_2 = EvidenceItem(
        evidence_id="ev_log_02",
        timestamp=datetime(2026, 11, 24, 12, 7, 0, tzinfo=timezone.utc),
        source="search_logs",
    )
    run_service.run_repo.observations[run_id] = [valid_obs_1, valid_obs_2]

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_orders_v130",
        evidence_ids=["ev_metric_01", "ev_log_02"],
        confidence=0.95,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    incident_window = (
        datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 24, 12, 20, 0, tzinfo=timezone.utc),
    )

    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        incident_window=incident_window,
    )

    assert updates["draft_diagnosis"]["root_cause_category"] == "bad_deploy"
    assert updates["draft_diagnosis"]["evidence_ids"] == ["ev_metric_01", "ev_log_02"]

    # Verify persisted in repo
    persisted = run_service.run_repo.get_run_diagnosis(TenantContext(tenant_id=UUID(base_state["tenant_id"])), run_id)
    assert persisted is not None
    assert persisted.status == "draft"
    assert persisted.root_cause_category == "bad_deploy"
    assert persisted.evidence_ids == ["ev_metric_01", "ev_log_02"]

    # Verify diagnosis_persisted event emitted
    persisted_events = [e for e in run_service.run_repo.events if e["event_type"] == "diagnosis_persisted"]
    assert len(persisted_events) == 1
    assert persisted_events[0]["payload"]["status"] == "draft"


# ---------------------------------------------------------------------------
# 5. Handoff Stub Reaches Terminal State
# ---------------------------------------------------------------------------
def test_handoff_stub_reaches_terminal_state(base_state: InvestigationState) -> None:
    run_service = MockRunService()
    run_id = UUID(base_state["run_id"])
    run_record = run_service.run_repo.get_by_id(TenantContext(tenant_id=UUID(base_state["tenant_id"])), run_id)
    assert run_record.status == "investigating"

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        evidence_ids=[],
        confidence=0.9,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    # Pass observations directly
    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        observations=[],
    )

    # Reaches escalated terminal status
    assert updates["status"] == "escalated"
    assert updates["handoff_result"]["target_status"] == "escalated"
    assert updates["handoff_result"]["reason"] == "investigation_complete_no_planner"
    assert run_record.status == "escalated"


# ---------------------------------------------------------------------------
# 6. Optimistic Locking Conflict Handled
# ---------------------------------------------------------------------------
def test_optimistic_locking_conflict_handled(base_state: InvestigationState) -> None:
    run_service = MockRunService()
    run_id = UUID(base_state["run_id"])
    tenant_ctx = TenantContext(tenant_id=UUID(base_state["tenant_id"]))
    run_record = run_service.run_repo.get_by_id(tenant_ctx, run_id)
    run_record.version = 5  # DB run is at version 5

    diag = Diagnosis(
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        evidence_ids=[],
        confidence=0.9,
    )
    base_state["draft_diagnosis"] = diag.model_dump()

    # Caller passes stale expected_version=2
    updates = finalize_node(
        state=base_state,
        run_service=run_service,  # type: ignore[arg-type]
        observations=[],
        expected_version=2,
    )

    # Does not crash; records conflict and sets failed
    assert updates["status"] == "failed"
    assert updates["last_error"]["code"] == "concurrent_modification"
    assert updates["last_error"]["expected_version"] == 2
    assert updates["last_error"]["actual_version"] == 5
