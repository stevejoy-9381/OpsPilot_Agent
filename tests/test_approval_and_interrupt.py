"""Tests for Feature 8: Approval Transaction, API Contract, Interrupt/Resume, and Worker Leases."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent.graph.nodes.await_approval import await_approval_node
from agent.graph.state import InvestigationState
from agent.worker import WorkerService
from apps.api.auth.dependencies import Principal, get_current_principal, get_db_session
from apps.api.domain.errors import (
    AlreadyDecidedError,
    ConcurrentModificationError,
    DecisionConflictError,
    ForbiddenError,
    IdempotencyConflictError,
    LeaseLostError,
    NotFoundError,
    PlanExpiredError,
    PlanHashMismatchError,
    SeparationOfDutiesError,
    StaleTargetError,
    TargetStateUnavailableError,
)
from apps.api.main import app
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalIdempotencyModel,
    ApprovalModel,
    IncidentModel,
    RunEventModel,
    RunModel,
    TenantModel,
)
from apps.api.routers.plans import get_target_reader
from apps.api.services.approval_service import ApprovalService
from shared.actions import ActionType
from shared.plans import PlanStatus, compute_action_key, plan_hash
from shared.targets import TargetState, TargetStateError, TargetStateErrorCode


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


def _sqlite_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _coerce_list_params(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> tuple[str, Any]:
        if isinstance(parameters, tuple):
            parameters = tuple(json.dumps(p) if isinstance(p, (list, dict)) else p for p in parameters)
        elif isinstance(parameters, list):
            parameters = [
                tuple(json.dumps(x) if isinstance(x, (list, dict)) else x for x in row)
                if isinstance(row, tuple)
                else (json.dumps(row) if isinstance(row, (list, dict)) else row)
                for row in parameters
            ]
        return statement, parameters

    @event.listens_for(engine, "connect")
    def _attach_app_schema(dbapi_conn: Any, _rec: Any) -> None:
        dbapi_conn.create_function(
            "clock_timestamp",
            0,
            lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
        )
        dbapi_conn.create_function("set_config", 3, lambda *args: "")
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS app")
        dbapi_conn.executescript(
            """
            CREATE TABLE app.tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.users (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.incidents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                alert_id TEXT NOT NULL,
                source TEXT NOT NULL,
                service TEXT NOT NULL,
                severity TEXT NOT NULL,
                alert_payload TEXT NOT NULL,
                opened_at TIMESTAMP NOT NULL,
                status_summary TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                case_id TEXT,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                resolution TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                remaining_calls INTEGER NOT NULL DEFAULT 8,
                max_calls INTEGER NOT NULL DEFAULT 8,
                last_error TEXT,
                heartbeat_at TIMESTAMP,
                worker_id TEXT,
                created_by_user_id TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            );
            CREATE TABLE app.run_leases (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_expires_at TIMESTAMP NOT NULL
            );
            CREATE TABLE app.action_plans (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                service TEXT,
                target_id TEXT,
                expected_version TEXT,
                rollback_to_version TEXT,
                rationale_evidence_ids TEXT NOT NULL DEFAULT '[]',
                rollback_route TEXT,
                plan_hash TEXT NOT NULL,
                policy_version TEXT,
                status TEXT NOT NULL DEFAULT 'proposed',
                notes TEXT NOT NULL DEFAULT '',
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.approvals (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                submitted_plan_hash TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                decided_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (plan_id, reviewer_id)
            );
            CREATE TABLE app.approval_idempotency (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                decision TEXT NOT NULL,
                submitted_plan_hash TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_body TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tenant_id, plan_id, reviewer_id, idempotency_key)
            );
            CREATE TABLE app.action_executions (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                action_key TEXT NOT NULL UNIQUE,
                target_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                heartbeat_at TIMESTAMP,
                refusal_code TEXT,
                last_error TEXT,
                target_state_before TEXT,
                target_state_after TEXT,
                dispatched_at TIMESTAMP,
                acknowledged_at TIMESTAMP,
                reconciled_at TIMESTAMP,
                completed_at TIMESTAMP,
                target_response TEXT,
                broker_response TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'broker',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (execution_id, seq)
            );
            CREATE TABLE app.run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (run_id, seq)
            );
            CREATE TABLE app.allowed_transitions (
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (from_status, to_status)
            );
            INSERT INTO app.allowed_transitions (from_status, to_status, description) VALUES
                ('queued', 'investigating', ''),
                ('investigating', 'needs_review', ''),
                ('investigating', 'escalated', ''),
                ('needs_review', 'approved', ''),
                ('needs_review', 'escalated', ''),
                ('approved', 'executing', ''),
                ('approved', 'escalated', ''),
                ('executing', 'verifying', ''),
                ('executing', 'escalated', ''),
                ('verifying', 'completed', ''),
                ('verifying', 'escalated', ''),
                ('escalated', 'investigating', '');
            CREATE TRIGGER app.trg_runs_bump_version
            AFTER UPDATE OF status ON runs
            FOR EACH ROW
            BEGIN
                UPDATE runs SET version = OLD.version + 1 WHERE id = NEW.id;
            END;
            """
        )

    return sessionmaker(bind=engine, expire_on_commit=False)


class _StubTargetReader:
    def __init__(
        self, version: str = "v1.1.0", fail: bool = False, age_seconds: int = 0
    ) -> None:
        self.version = version
        self.fail = fail
        self.age_seconds = age_seconds

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        if self.fail:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=target_id,
                message="lab unreachable",
            )
        return TargetState(
            target_id=target_id,
            current_version=self.version,
            previous_version="v1.0.0",
            source="test_stub",
            read_at=as_of - timedelta(seconds=self.age_seconds),
        )


def _seed_proposed_plan(
    session: Session,
    *,
    expired: bool = False,
    created_by_user_id: UUID | None = None,
    run_status: str = "needs_review",
    run_version: int = 3,
) -> tuple[UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now - timedelta(minutes=1) if expired else now + timedelta(minutes=15)

    session.add(TenantModel(id=tenant_id, name="Test Tenant", slug=f"t-{tenant_id.hex[:8]}"))
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_1",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={"service": "orders"},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status=run_status,
            version=run_version,
            remaining_calls=5,
            max_calls=8,
            created_by_user_id=created_by_user_id,
        )
    )
    phash = plan_hash(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        target_id="lab-orders",
        expected_version="v1.1.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=["ev_1"],
        expires_at=expires_at,
    )
    session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="orders",
            target_id="lab-orders",
            expected_version="v1.1.0",
            rollback_to_version="v1.0.0",
            rationale_evidence_ids=["ev_1"],
            rollback_route="Re-deploy orders v1.1.0",
            plan_hash=phash,
            policy_version="v1",
            status=PlanStatus.PROPOSED.value,
            notes="pool leak",
            expires_at=expires_at,
        )
    )
    session.commit()
    return tenant_id, run_id, plan_id, phash


def test_compute_action_key_deterministic() -> None:
    pid = UUID("550e8400-e29b-41d4-a716-446655440000")
    ph = "a" * 64
    k1 = compute_action_key(pid, ph)
    k2 = compute_action_key(str(pid).upper(), ph.upper())
    assert k1 == k2
    assert len(k1) == 64


def test_approve_happy_path_and_idempotent_replay() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    reviewer_id = uuid4()
    principal = Principal(tenant_id=tenant_id, user_id=reviewer_id, role="reviewer")

    svc = ApprovalService(session, target_reader=_StubTargetReader("v1.1.0"))
    resp = svc.decide_plan(
        principal=principal,
        plan_id=plan_id,
        decision="approve",
        submitted_plan_hash=phash,
        reason="Verified logs",
        if_match_version=3,
        idempotency_key="idem-1",
    )

    assert resp.decision == "approve"
    assert resp.plan_status == "approved"
    assert resp.run_status == "approved"
    assert resp.action_execution_id is not None
    assert resp.action_key == compute_action_key(plan_id, phash)
    assert resp.idempotent_replay is False

    exec_row = session.scalar(select(ActionExecutionModel).where(ActionExecutionModel.plan_id == plan_id))
    assert exec_row is not None
    assert exec_row.status == "queued"
    assert exec_row.action_key == resp.action_key

    events = list(session.scalars(select(RunEventModel).where(RunEventModel.run_id == run_id)).all())
    assert any(e.type == "approval_recorded" for e in events)

    # Same reviewer + same decision + same Idempotency-Key -> replay
    replay = svc.decide_plan(
        principal=principal,
        plan_id=plan_id,
        decision="approve",
        submitted_plan_hash=phash,
        reason="Verified logs",
        if_match_version=3,
        idempotency_key="idem-1",
    )
    assert replay.idempotent_replay is True
    assert replay.approval_id == resp.approval_id
    assert replay.action_execution_id == resp.action_execution_id

    # Same reviewer + different decision with SAME Idempotency-Key -> IdempotencyConflictError (409)
    with pytest.raises(IdempotencyConflictError):
        svc.decide(
            principal=principal,
            plan_id=plan_id,
            decision="reject",
            submitted_plan_hash=phash,
            reason="changed mind",
            run_version=3,
            idempotency_key="idem-1",
        )

    # Same reviewer + different decision with DIFFERENT Idempotency-Key -> DecisionConflictError (409)
    with pytest.raises(DecisionConflictError):
        svc.decide(
            principal=principal,
            plan_id=plan_id,
            decision="reject",
            submitted_plan_hash=phash,
            reason="changed mind",
            run_version=3,
            idempotency_key="idem-2",
        )

    # Different reviewer -> AlreadyDecidedError (409)
    other_reviewer = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")
    with pytest.raises(AlreadyDecidedError):
        svc.decide_plan(
            principal=other_reviewer,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="second approval",
            if_match_version=3,
            idempotency_key="idem-3",
        )


def test_reject_path_transitions_run_to_escalated() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    svc = ApprovalService(session)
    resp = svc.decide_plan(
        principal=principal,
        plan_id=plan_id,
        decision="reject",
        submitted_plan_hash=phash,
        reason="Wrong service targeted",
        if_match_version=3,
        idempotency_key="idem-rej-1",
    )
    assert resp.plan_status == "rejected"
    assert resp.run_status == "escalated"
    assert resp.action_execution_id is None

    run_row = session.get(RunModel, run_id)
    assert run_row is not None
    assert run_row.status == "escalated"
    assert run_row.last_error is not None
    assert run_row.last_error["code"] == "reviewer_rejected"


def test_stale_target_supersedes_plan_and_escalates_run() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    svc = ApprovalService(session, target_reader=_StubTargetReader("v1.2.0"))
    with pytest.raises(StaleTargetError):
        svc.decide_plan(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="approve",
            if_match_version=3,
            idempotency_key="idem-stale",
        )

    plan_row = session.get(ActionPlanModel, plan_id)
    run_row = session.get(RunModel, run_id)
    assert plan_row is not None and plan_row.status == "superseded"
    assert run_row is not None and run_row.status == "escalated"


def test_expired_plan_returns_410_and_escalates() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session, expired=True)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    svc = ApprovalService(session, target_reader=_StubTargetReader("v1.1.0"))
    with pytest.raises(PlanExpiredError):
        svc.decide_plan(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="late",
            if_match_version=3,
            idempotency_key="idem-exp",
        )

    plan_row = session.get(ActionPlanModel, plan_id)
    run_row = session.get(RunModel, run_id)
    assert plan_row is not None and plan_row.status == "expired"
    assert run_row is not None and run_row.status == "escalated"


def test_plan_hash_mismatch_if_match_sod_and_auth_checks() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    creator_id = uuid4()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(
        session, created_by_user_id=creator_id
    )

    # Service principal forbidden
    svc_principal = Principal(tenant_id=tenant_id, user_id=None, role="worker", is_service=True)
    svc = ApprovalService(
        session,
        target_reader=_StubTargetReader("v1.1.0"),
        enforce_separation_of_duties=True,
    )
    with pytest.raises(ForbiddenError):
        svc.decide_plan(
            principal=svc_principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="",
            if_match_version=3,
            idempotency_key="k1",
        )

    # Hash mismatch
    reviewer = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")
    with pytest.raises(PlanHashMismatchError):
        svc.decide_plan(
            principal=reviewer,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash="0" * 64,
            reason="",
            if_match_version=3,
            idempotency_key="k2",
        )

    # ConcurrentModificationError on wrong If-Match
    with pytest.raises(ConcurrentModificationError):
        svc.decide_plan(
            principal=reviewer,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="",
            if_match_version=99,
            idempotency_key="k3",
        )

    # Separation of duties violation when creator == reviewer
    creator_reviewer = Principal(tenant_id=tenant_id, user_id=creator_id, role="reviewer")
    with pytest.raises(SeparationOfDutiesError):
        svc.decide_plan(
            principal=creator_reviewer,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="",
            if_match_version=3,
            idempotency_key="k4",
        )

    # Cross-tenant -> NotFoundError
    other_tenant_reviewer = Principal(tenant_id=uuid4(), user_id=uuid4(), role="reviewer")
    with pytest.raises(NotFoundError):
        svc.decide_plan(
            principal=other_tenant_reviewer,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="",
            if_match_version=3,
            idempotency_key="k5",
        )


def test_post_decisions_api_contract_and_http_codes() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    def _override_db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_target_reader] = lambda: _StubTargetReader("v1.1.0")
    try:
        client = TestClient(app)
        # Missing If-Match -> 428
        r_428 = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "Idempotency-Key": "api-1"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_428.status_code == 428

        # Missing Idempotency-Key -> 400
        r_400 = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_400.status_code == 400

        # Valid approve -> 200
        r_200 = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "api-1"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_200.status_code == 200
        data = r_200.json()
        assert data["plan_status"] == "approved"
        assert data["run_status"] == "approved"
        assert data["idempotent_replay"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_decision"),
    [
        ("approved", "approved", "approved"),
        ("rejected", "escalated", "rejected"),
        ("expired", "escalated", "expired"),
        ("cancelled", "escalated", "cancelled"),
    ],
)
def test_await_approval_interrupt_and_command_resume(
    outcome: str, expected_status: str, expected_decision: str
) -> None:
    builder = StateGraph(InvestigationState)
    builder.add_node("await_approval", await_approval_node)
    builder.set_entry_point("await_approval")
    builder.add_edge("await_approval", END)
    graph = builder.compile(checkpointer=MemorySaver())

    run_id = str(uuid4())
    config = {"configurable": {"thread_id": run_id}}
    graph.invoke(
        {
            "run_id": run_id,
            "proposed_plan_id": "plan_123",
            "proposed_plan_hash": "a" * 64,
            "proposed_action": "rollback_service",
            "proposed_service": "orders",
            "proposed_target_id": "lab-orders",
            "proposed_expected_version": "v1.1.0",
            "proposed_rollback_to_version": "v1.0.0",
            "proposed_expires_at": "2026-11-25T17:00:00Z",
        },
        config=config,
    )
    snap = graph.get_state(config)
    assert snap.next == ("await_approval",)
    assert snap.tasks
    assert snap.tasks[0].interrupts[0].value["plan_id"] == "plan_123"

    resumed = WorkerService.resume_interrupted_run(
        graph,
        run_id,
        {"outcome": outcome, "plan_id": "plan_123", "action_execution_id": "exec_1"},
    )
    assert resumed["status"] == expected_status
    assert resumed["approval_decision"] == expected_decision


def test_worker_lease_reclaim_and_zombie_fencing() -> None:
    from apps.api.domain.context import TenantContext
    from apps.api.services.run_service import FencedRunService, RunService

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    now = datetime.now(timezone.utc)

    session.add(TenantModel(id=tenant_id, name="T", slug=f"t-{tenant_id.hex[:6]}"))
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_lease",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
    )
    session.commit()

    w1 = WorkerService(session_factory=SessionFactory, worker_id="worker-1", lease_ttl_seconds=30.0)
    claim1 = w1.claim_next_run(session)
    assert claim1 is not None
    _, _, _, _, token1 = claim1

    # Move run to investigating and expire w1's lease
    run_row = session.get(RunModel, run_id)
    assert run_row is not None
    assert run_row.lease_owner == "worker-1"
    run_row.status = "investigating"
    run_row.lease_expires_at = now - timedelta(seconds=5)
    session.commit()

    # Two workers racing: w2 reclaims, w3 sees nothing claimable
    w2 = WorkerService(session_factory=SessionFactory, worker_id="worker-2", lease_ttl_seconds=30.0)
    w3 = WorkerService(session_factory=SessionFactory, worker_id="worker-3", lease_ttl_seconds=30.0)
    claim2 = w2.claim_next_run(session)
    assert claim2 is not None
    reclaimed_run_id, _, reclaimed_status, _, token2 = claim2
    assert reclaimed_run_id == run_id
    assert reclaimed_status == "investigating"
    assert token2 != token1
    assert w3.claim_next_run(session) is None

    # Zombie w1 wakes up and tries a state-changing write via FencedRunService -> rejected!
    zombie_svc = FencedRunService(RunService(session), lease_token=token1)
    ctx = TenantContext(tenant_id=tenant_id, role="worker")
    with pytest.raises(LeaseLostError) as exc_info:
        zombie_svc.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="escalated",
            actor="worker:worker-1",
            expected_version=2,
        )
    print(f"\nZOMBIE_WRITE_REJECTION: {type(exc_info.value).__name__}: {exc_info.value.message}")

    # Record worker_lease_lost event as worker exception handler does
    RunService(session).append_event(
        ctx=ctx,
        run_id=run_id,
        event_type="worker_lease_lost",
        actor="worker:worker-1",
        payload={"previous_owner": "worker-1"},
    )
    session.commit()

    # w2 heartbeat with token2 succeeds
    w2.update_heartbeat(session, run_id, token2)
    events = list(
        session.scalars(
            select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.seq.asc())
        ).all()
    )
    assert [e.type for e in events] == ["worker_claimed", "run_reclaimed", "worker_lease_lost"]
    assert events[1].payload["previous_owner"] == "worker-1"
    print("RECLAIM_EVENT_SEQUENCE:")
    for e in events:
        print(f"  seq={e.seq} type={e.type} actor={e.actor} payload={json.dumps(e.payload)}")


def test_heartbeat_keeps_slow_node_alive_and_sigterm_shutdown() -> None:
    import threading
    import time

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    now = datetime.now(timezone.utc)

    session.add(TenantModel(id=tenant_id, name="T", slug=f"t-{tenant_id.hex[:6]}"))
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_hb",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
    )
    session.commit()

    w = WorkerService(session_factory=SessionFactory, worker_id="hb-worker", lease_ttl_seconds=0.4)
    w.heartbeat_seconds = 0.08
    claim = w.claim_next_run(session)
    assert claim is not None
    _, _, _, _, token = claim

    initial_exp = session.get(RunModel, run_id).lease_expires_at  # type: ignore[union-attr]
    stop_ev = threading.Event()
    t = w.start_heartbeat_thread(run_id, token, stop_ev)
    time.sleep(0.35)
    stop_ev.set()
    t.join(timeout=1.0)

    session.expire_all()
    extended_exp = session.get(RunModel, run_id).lease_expires_at  # type: ignore[union-attr]
    assert extended_exp > initial_exp  # type: ignore[operator]

    # SIGTERM stops claiming and release_lease clears lease columns
    w.handle_sigterm()
    assert w._running is False
    assert w.run_once() is False
    w.release_lease(session, run_id, token)
    session.expire_all()
    run_row = session.get(RunModel, run_id)
    assert run_row is not None
    assert run_row.lease_owner is None
    assert run_row.lease_token is None
    assert run_row.lease_expires_at is None


def test_target_reader_unavailable_stale_and_tamper_detection() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    # Fail closed when target_reader is None
    with pytest.raises(TargetStateUnavailableError):
        ApprovalService(session, target_reader=None).decide(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="ok",
            run_version=3,
            idempotency_key="t-none",
        )

    # Fail closed when target_reader returns TargetStateError
    with pytest.raises(TargetStateUnavailableError):
        ApprovalService(session, target_reader=_StubTargetReader(fail=True)).decide(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="ok",
            run_version=3,
            idempotency_key="t-err",
        )

    # Fail closed when target_state.read_at exceeds TARGET_STATE_MAX_AGE_SECONDS
    with pytest.raises(TargetStateUnavailableError):
        ApprovalService(
            session,
            target_reader=_StubTargetReader(version="v1.1.0", age_seconds=60),
            target_state_max_age_seconds=30,
        ).decide(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="ok",
            run_version=3,
            idempotency_key="t-stale",
        )

    # Tampered stored plan field detected by recompute check
    plan_row = session.get(ActionPlanModel, plan_id)
    assert plan_row is not None
    plan_row.service = "inventory"
    session.commit()

    with pytest.raises(PlanHashMismatchError):
        ApprovalService(session, target_reader=_StubTargetReader("v1.1.0")).decide(
            principal=principal,
            plan_id=plan_id,
            decision="approve",
            submitted_plan_hash=phash,
            reason="ok",
            run_version=3,
            idempotency_key="t-tamper",
        )


def test_partial_failure_rollback_leaves_zero_partial_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injects a failure at each write step and asserts 0 partial rows survive rollback."""
    steps = [
        "after_approval_flush",
        "after_execution_flush",
        "run_transition",
        "append_event",
        "idempotency_flush",
    ]
    for step in steps:
        SessionFactory = _sqlite_session_factory()
        session = SessionFactory()
        tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
        principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")
        svc = ApprovalService(session, target_reader=_StubTargetReader("v1.1.0"))

        orig_flush = session.flush
        flush_calls = 0

        def _failing_flush(
            *args: Any,
            _orig: Any = orig_flush,
            _step: str = step,
            **kwargs: Any,
        ) -> Any:
            nonlocal flush_calls
            flush_calls += 1
            _orig(*args, **kwargs)
            if _step == "after_approval_flush" and flush_calls == 1:
                raise RuntimeError("injected failure after approval flush")
            if _step == "after_execution_flush" and flush_calls == 3:
                raise RuntimeError("injected failure after execution flush")

        if step in ("after_approval_flush", "after_execution_flush"):
            monkeypatch.setattr(session, "flush", _failing_flush)
        elif step == "run_transition":
            monkeypatch.setattr(
                svc.run_service,
                "transition",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected transition failure")),
            )
        elif step == "append_event":
            monkeypatch.setattr(
                svc.run_service,
                "append_event",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected event failure")),
            )
        elif step == "idempotency_flush":
            orig_add = session.add

            def _fail_idem_add(obj: Any, _orig_add: Any = orig_add) -> None:
                if isinstance(obj, ApprovalIdempotencyModel):
                    raise RuntimeError("injected idempotency failure")
                _orig_add(obj)

            monkeypatch.setattr(session, "add", _fail_idem_add)

        with pytest.raises(RuntimeError):
            svc.decide(
                principal=principal,
                plan_id=plan_id,
                decision="approve",
                submitted_plan_hash=phash,
                reason="ok",
                run_version=3,
                idempotency_key=f"fail-{step}",
            )

        assert session.query(ApprovalModel).filter_by(plan_id=plan_id).count() == 0
        assert session.query(ActionExecutionModel).filter_by(plan_id=plan_id).count() == 0
        assert session.query(ApprovalIdempotencyModel).filter_by(plan_id=plan_id).count() == 0
        assert session.query(RunEventModel).filter_by(run_id=run_id).count() == 0
        plan_row = session.get(ActionPlanModel, plan_id)
        run_row = session.get(RunModel, run_id)
        assert plan_row is not None and plan_row.status == "proposed"
        assert run_row is not None and run_row.status == "needs_review" and run_row.version == 3
        monkeypatch.undo()


def test_route_role_table_csrf_identity_rejection_and_get_decision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.api.routers.plans import plans_rate_limiter

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, _, plan_id, phash = _seed_proposed_plan(session)
    reviewer_id = uuid4()

    def _override_db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_target_reader] = lambda: _StubTargetReader("v1.1.0")
    try:
        client = TestClient(app)

        # 1. Viewer -> 403
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=tenant_id, user_id=uuid4(), role="viewer"
        )
        r_viewer = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r1"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_viewer.status_code == 403

        # 2. Worker service principal -> 403
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=tenant_id, user_id=None, role="worker", is_service=True
        )
        r_worker = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"If-Match": "3", "Idempotency-Key": "r2"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_worker.status_code == 403

        # 3. Cross-tenant reviewer -> 404
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=uuid4(), user_id=uuid4(), role="reviewer"
        )
        r_xtenant = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r3"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_xtenant.status_code == 404

        # 4. Reviewer missing CSRF -> 403
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=tenant_id, user_id=reviewer_id, role="reviewer"
        )
        r_nocsrf = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"If-Match": "3", "Idempotency-Key": "r4"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
        )
        assert r_nocsrf.status_code == 403

        # 5. Body / query / header with reviewer_id -> 422
        r_body_id = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r5"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reviewer_id": str(uuid4())},
        )
        assert r_body_id.status_code == 422

        r_query_id = client.post(
            f"/plans/{plan_id}/decisions?reviewer_id={uuid4()}",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r6"},
            json={"decision": "approve", "submitted_plan_hash": phash},
        )
        assert r_query_id.status_code == 422

        r_header_id = client.post(
            f"/plans/{plan_id}/decisions",
            headers={
                "X-Requested-With": "OpsPilotApp",
                "If-Match": "3",
                "Idempotency-Key": "r7",
                "X-Reviewer-Id": str(uuid4()),
            },
            json={"decision": "approve", "submitted_plan_hash": phash},
        )
        assert r_header_id.status_code == 422

        # 6. Reject without reason -> 422
        r_rej_empty = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r8"},
            json={"decision": "reject", "submitted_plan_hash": phash, "reason": "   "},
        )
        assert r_rej_empty.status_code == 422

        # 7. Wrong hash -> 409 plan_hash_mismatch
        r_badhash = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r9"},
            json={"decision": "approve", "submitted_plan_hash": "0" * 64, "reason": "secret-reason"},
        )
        assert r_badhash.status_code == 409
        assert r_badhash.json()["error"]["code"] == "plan_hash_mismatch"

        # 8. Approve + GET /plans/{id}/decision + redacted audit log check
        caplog.set_level("INFO", logger="opspilot.audit.plan_decisions")
        secret_reason = "CONFIDENTIAL_REASON_TEXT_XYZ"
        r_ok = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r-ok"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": secret_reason},
        )
        assert r_ok.status_code == 200
        assert "CONFIDENTIAL_REASON_TEXT_XYZ" not in caplog.text
        assert "pool leak" not in caplog.text
        assert f"plan_id={plan_id}" in caplog.text
        assert "outcome_code=approved" in caplog.text

        # GET /plans/{id}/decision
        r_get = client.get(f"/plans/{plan_id}/decision")
        assert r_get.status_code == 200
        assert r_get.json()["decision"] == "approve"
        assert r_get.json()["reviewer_id"] == str(reviewer_id)

        # Replay with same key -> 200 idempotent_replay=True
        r_rep = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r-ok"},
            json={"decision": "approve", "submitted_plan_hash": phash, "reason": secret_reason},
        )
        assert r_rep.status_code == 200
        assert r_rep.json()["idempotent_replay"] is True

        # 9. Per-reviewer rate limit -> 429
        rl_key = f"decisions:reviewer:{tenant_id}:{reviewer_id}"
        for _ in range(35):
            plans_rate_limiter.record_attempt(rl_key)
        r_rl = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "r-rl"},
            json={"decision": "approve", "submitted_plan_hash": phash},
        )
        assert r_rl.status_code == 429
        plans_rate_limiter.reset(rl_key)
    finally:
        app.dependency_overrides.clear()


def test_openapi_snapshot_includes_decisions_and_error_examples() -> None:
    schema = app.openapi()
    post_op = schema["paths"]["/plans/{id}/decisions"]["post"]
    get_op = schema["paths"]["/plans/{id}/decision"]["get"]
    assert get_op["responses"]["200"] is not None
    for code in ("200", "400", "401", "403", "404", "409", "410", "413", "422", "428", "429", "503"):
        assert code in post_op["responses"], f"Missing OpenAPI response for {code}"


def test_sweeper_idempotence_and_cancelled_run_rejection() -> None:
    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session, expired=True)

    worker = WorkerService(session_factory=SessionFactory, worker_id="sweeper-1")
    first = worker.sweep_expired_plans()
    assert first == [plan_id]

    session.expire_all()
    plan_row = session.get(ActionPlanModel, plan_id)
    run_row = session.get(RunModel, run_id)
    assert plan_row is not None and plan_row.status == "expired"
    assert run_row is not None and run_row.status == "escalated"

    # Second sweep is a no-op (idempotent)
    second = worker.sweep_expired_plans()
    assert second == []

    # Cancelled / escalated run cannot be approved
    from apps.api.domain.errors import RunNotInReviewError

    t2, _, p2, h2 = _seed_proposed_plan(session, run_status="escalated")
    rev2 = Principal(tenant_id=t2, user_id=uuid4(), role="reviewer")
    with pytest.raises(RunNotInReviewError):
        ApprovalService(session, target_reader=_StubTargetReader("v1.1.0")).decide(
            principal=rev2,
            plan_id=p2,
            decision="approve",
            submitted_plan_hash=h2,
            reason="ok",
            run_version=3,
            idempotency_key="cancelled-1",
        )


def test_db_clock_vs_app_clock_skew(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import apps.api.services.approval_service as approval_mod

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, _, plan_id, phash = _seed_proposed_plan(session, expired=False)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    class _SkewedDatetimeForward(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return real_now + timedelta(hours=10)

    class _SkewedDatetimeBackward(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return real_now - timedelta(hours=10)

    real_now = datetime.now(timezone.utc)
    monkeypatch.setattr(approval_mod, "datetime", _SkewedDatetimeForward)
    caplog.set_level("WARNING", logger="opspilot.api.approval")
    delta_fwd = approval_mod.check_clock_skew(session, warn_threshold_seconds=5.0)
    assert abs(delta_fwd) > 30000
    assert "clock_skew_detected" in caplog.text

    # Approval still succeeds when app clock is +10h because decisions use ONLY the DB clock
    res = approval_mod.ApprovalService(
        session, target_reader=_StubTargetReader("v1.1.0")
    ).decide(
        principal=principal,
        plan_id=plan_id,
        decision="approve",
        submitted_plan_hash=phash,
        reason="ok",
        run_version=3,
        idempotency_key="skew-fwd",
    )
    assert res.plan_status == "approved"

    # Also verify -10h backward skew does not allow approving an already-expired plan
    t2, _, p2, h2 = _seed_proposed_plan(session, expired=True)
    rev2 = Principal(tenant_id=t2, user_id=uuid4(), role="reviewer")
    monkeypatch.setattr(approval_mod, "datetime", _SkewedDatetimeBackward)
    with pytest.raises(PlanExpiredError):
        approval_mod.ApprovalService(
            session, target_reader=_StubTargetReader("v1.1.0")
        ).decide(
            principal=rev2,
            plan_id=p2,
            decision="approve",
            submitted_plan_hash=h2,
            reason="ok",
            run_version=3,
            idempotency_key="skew-bwd",
        )


def test_expiry_race_200_iterations_zero_double_outcomes() -> None:
    import concurrent.futures
    import threading

    SessionFactory = _sqlite_session_factory()
    approved_count = 0
    expired_count = 0
    iterations = 200
    db_lock = threading.Lock()

    for i in range(iterations):
        with SessionFactory() as setup_s:
            tenant_id, run_id, plan_id, phash = _seed_proposed_plan(setup_s, expired=True)

        principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")
        worker = WorkerService(session_factory=SessionFactory, worker_id=f"race-w-{i}")

        def _decide(
            _p: Principal = principal,
            _pid: UUID = plan_id,
            _ph: str = phash,
            _idx: int = i,
        ) -> str:
            with db_lock:
                with SessionFactory() as s:
                    try:
                        ApprovalService(s, target_reader=_StubTargetReader("v1.1.0")).decide(
                            principal=_p,
                            plan_id=_pid,
                            decision="approve",
                            submitted_plan_hash=_ph,
                            reason="race",
                            run_version=3,
                            idempotency_key=f"race-{_idx}",
                        )
                        return "approved"
                    except PlanExpiredError:
                        return "expired"

        def _sweep(_w: WorkerService = worker) -> list[UUID]:
            with db_lock:
                return _w.sweep_expired_plans()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_dec = pool.submit(_decide)
            f_swp = pool.submit(_sweep)
            dec_res = f_dec.result()
            swp_res = f_swp.result()

        with SessionFactory() as check_s:
            plan_row = check_s.get(ActionPlanModel, plan_id)
            exec_cnt = check_s.query(ActionExecutionModel).filter_by(plan_id=plan_id).count()
            assert plan_row is not None
            assert plan_row.status in ("approved", "expired")
            if plan_row.status == "approved":
                assert exec_cnt == 1
                approved_count += 1
            else:
                assert exec_cnt == 0
                assert dec_res == "expired" or plan_id in swp_res
                expired_count += 1

    assert approved_count + expired_count == iterations
    print(f"EXPIRY_RACE_SUMMARY: iterations={iterations} approved={approved_count} expired={expired_count}")


def test_await_review_restart_double_resume_and_post_resume_routing() -> None:
    from agent.graph.graph import route_after_await_review
    from agent.graph.nodes.await_review import await_execution_node, await_review_node

    saver = MemorySaver()
    handoff_calls: list[str] = []

    class _StubExecutionPort:
        def handoff_approved_plan(
            self, *, run_id: str, plan_id: str, action_execution_id: str | None
        ) -> dict[str, Any]:
            handoff_calls.append(f"{run_id}:{plan_id}:{action_execution_id}")
            return {"handed_off": True}

    def _build_mini_graph(cp: MemorySaver) -> Any:
        b = StateGraph(InvestigationState)
        b.add_node("await_review", await_review_node)
        b.add_node(
            "await_execution",
            lambda s: await_execution_node(s, execution_port=_StubExecutionPort()),
        )
        b.set_entry_point("await_review")
        b.add_conditional_edges(
            "await_review",
            route_after_await_review,
            {"await_execution": "await_execution", "end": END},
        )
        b.add_edge("await_execution", END)
        return b.compile(checkpointer=cp)

    # 1. Graph 1 pauses at await_review and persists checkpoint
    g1 = _build_mini_graph(saver)
    run_id = str(uuid4())
    cfg = {"configurable": {"thread_id": run_id}}
    g1.invoke(
        {
            "run_id": run_id,
            "proposed_plan_id": "plan_r1",
            "proposed_plan_hash": "b" * 64,
            "proposed_expires_at": "2026-11-25T17:00:00Z",
        },
        config=cfg,
    )

    # 2. Process-local restart: brand-new compiled graph object with same checkpointer & thread_id
    g2 = _build_mini_graph(saver)
    snap = g2.get_state(cfg)
    assert snap.next == ("await_review",)
    assert snap.tasks[0].interrupts[0].value == {
        "plan_id": "plan_r1",
        "plan_hash": "b" * 64,
        "expires_at": "2026-11-25T17:00:00Z",
    }

    resumed = WorkerService.resume_interrupted_run(
        g2, run_id, {"outcome": "approved", "plan_id": "plan_r1", "action_execution_id": "exec_9"}
    )
    assert resumed is not None
    assert resumed["status"] == "approved"
    assert handoff_calls == [f"{run_id}:plan_r1:exec_9"]

    # 3. Double resume is a no-op
    second = WorkerService.resume_interrupted_run(
        g2, run_id, {"outcome": "approved", "plan_id": "plan_r1", "action_execution_id": "exec_9"}
    )
    assert second is None
    assert len(handoff_calls) == 1


def test_pre_interrupt_idempotency_rerun_5_times() -> None:
    from langgraph.errors import GraphInterrupt

    from agent.graph.nodes.await_review import await_review_node

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    read_calls = 0

    def _readonly_reader(tid: str | None, pid: str | None) -> dict[str, Any] | None:
        nonlocal read_calls
        read_calls += 1
        row = session.get(ActionPlanModel, UUID(str(pid)))
        return {"status": row.status} if row else None

    state: InvestigationState = {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "proposed_plan_id": str(plan_id),
        "proposed_plan_hash": phash,
        "proposed_expires_at": "2026-11-25T17:00:00Z",
    }

    builder = StateGraph(InvestigationState)
    builder.add_node("await_review", lambda s: await_review_node(s, plan_reader=_readonly_reader))
    builder.set_entry_point("await_review")
    builder.add_edge("await_review", END)

    for i in range(5):
        g = builder.compile(checkpointer=MemorySaver())
        g.invoke(state, config={"configurable": {"thread_id": f"idem-{i}"}})

    assert read_calls == 5
    assert session.query(ApprovalModel).count() == 0
    assert session.query(ActionExecutionModel).count() == 0
    assert session.query(RunEventModel).count() == 0
    run_row = session.get(RunModel, run_id)
    assert run_row is not None and run_row.status == "needs_review" and run_row.version == 3
    _ = GraphInterrupt


def test_checkpoint_state_size_and_no_secrets() -> None:
    from agent.graph.nodes.await_review import await_review_node
    from agent.graph.state import MAX_WORST_CASE_STATE_BYTES
    from tools.common.sanitize import (
        _BEARER_PATTERN,
        _CONN_STRING_PATTERN,
        _JWT_PATTERN,
        _KEY_VALUE_SECRET_PATTERN,
        _PREFIXED_KEY_PATTERN,
    )

    saver = MemorySaver()
    b = StateGraph(InvestigationState)
    b.add_node("await_review", await_review_node)
    b.set_entry_point("await_review")
    b.add_edge("await_review", END)
    g = b.compile(checkpointer=saver)

    run_id = str(uuid4())
    worst_case: InvestigationState = {
        "run_id": run_id,
        "tenant_id": str(uuid4()),
        "incident_id": str(uuid4()),
        "status": "needs_review",
        "evidence_ids": [f"ev_{i}" for i in range(8)],
        "tool_trace": [{"tool": "get_metrics", "evidence_id": f"ev_{i}"} for i in range(8)],
        "hypotheses_ledger": {
            f"hyp_0{i}": {"category": "bad_deploy", "status": "refuted", "confidence": 0.2}
            for i in range(1, 5)
        },
        "proposed_plan_id": "plan_size_1",
        "proposed_plan_hash": "c" * 64,
        "proposed_action": "rollback_service",
        "proposed_service": "orders",
        "proposed_target_id": "lab-orders",
        "proposed_expected_version": "v1.1.0",
        "proposed_rollback_to_version": "v1.0.0",
        "proposed_expires_at": "2026-11-25T17:00:00Z",
    }
    cfg = {"configurable": {"thread_id": run_id}}
    g.invoke(worst_case, config=cfg)
    snap = g.get_state(cfg)
    serialized = json.dumps(snap.values, sort_keys=True)
    assert len(serialized.encode("utf-8")) < MAX_WORST_CASE_STATE_BYTES
    for pat in (
        _BEARER_PATTERN,
        _JWT_PATTERN,
        _CONN_STRING_PATTERN,
        _KEY_VALUE_SECRET_PATTERN,
        _PREFIXED_KEY_PATTERN,
    ):
        assert not pat.search(serialized)


def test_worker_resume_driver_and_event_table() -> None:
    from agent.graph.graph import route_after_await_review
    from agent.graph.nodes.await_review import await_execution_node, await_review_node

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    tenant_id, run_id, plan_id, phash = _seed_proposed_plan(session)
    principal = Principal(tenant_id=tenant_id, user_id=uuid4(), role="reviewer")

    saver = MemorySaver()
    b = StateGraph(InvestigationState)
    b.add_node("await_review", await_review_node)
    b.add_node("await_execution", await_execution_node)
    b.set_entry_point("await_review")
    b.add_conditional_edges(
        "await_review",
        route_after_await_review,
        {"await_execution": "await_execution", "end": END},
    )
    b.add_edge("await_execution", END)
    g = b.compile(checkpointer=saver)

    # 1. Pause graph at await_review
    g.invoke(
        {
            "run_id": str(run_id),
            "tenant_id": str(tenant_id),
            "proposed_plan_id": str(plan_id),
            "proposed_plan_hash": phash,
            "proposed_expires_at": "2026-11-25T17:00:00Z",
        },
        config={"configurable": {"thread_id": str(run_id)}},
    )

    # 2. Approve via ApprovalService
    ApprovalService(session, target_reader=_StubTargetReader("v1.1.0")).decide(
        principal=principal,
        plan_id=plan_id,
        decision="approve",
        submitted_plan_hash=phash,
        reason="verified",
        run_version=3,
        idempotency_key="drv-1",
    )

    # 3. Worker resume driver resumes from DB decision
    worker = WorkerService(session_factory=SessionFactory, worker_id="resume-drv-1")
    resumed = worker.resume_pending_decisions(graph_override=g)
    assert resumed == [run_id]

    # 4. Double pass is a no-op
    assert worker.resume_pending_decisions(graph_override=g) == []

    events = list(
        session.scalars(
            select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.seq.asc())
        ).all()
    )
    assert [e.type for e in events] == ["status_transition", "approval_recorded", "graph_resumed"]
    print("\nRUN_EVENTS_TABLE:")
    for e in events:
        print(f"  seq={e.seq} type={e.type} actor={e.actor} payload={json.dumps(e.payload)}")
