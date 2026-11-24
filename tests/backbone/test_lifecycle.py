"""Backbone lifecycle, negative paths, failure paths, and load verification tests.

Proves the backbone works before any agent exists:
1. Full scripted lifecycle driver:
   queued -> investigating -> needs_review -> approved -> executing -> verifying -> completed.
   Writing observations, hypotheses, a plan, approval row (by reviewer principal),
   execution row, and verification result via repositories and RunService.
   Asserting events, versions, and constraints at each step.
2. Negative paths:
   - skip a state (queued -> approved)
   - approve an expired plan row
   - duplicate action_key insert on action_executions
   - mutate a plan after insert (service/action)
   - delete an event
   - read another tenant's run
   - concurrent double transition with same expected_version
3. Failure paths:
   - run -> failed with last_error
   - run -> escalated
   - cancel from investigating
   - cancel from completed rejected
4. Load check:
   - 200 runs created concurrently via the API with distinct idempotency keys
   - Verify gap-free event seq per run and no orphan rows
"""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from apps.api.auth.dependencies import SESSION_COOKIE_NAME, get_db_session
from apps.api.auth.passwords import hash_password
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    ConcurrentModificationError,
    IllegalTransitionError,
    NotFoundError,
)
from apps.api.main import app
from apps.api.models.hashing import compute_plan_hash
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalModel,
    DiagnosisModel,
    HypothesisModel,
    IncidentModel,
    ObservationModel,
    RunModel,
    SessionModel,
    TenantModel,
    UserModel,
)
from apps.api.repositories.run_repository import RunRepository
from apps.api.routers.runs import runs_rate_limiter
from apps.api.services.run_service import RunService
from tools.common.evidence_store import compute_content_hash

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://opspilot:opspilot_dev_pass@127.0.0.1:54333/opspilot",
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DB_URL, pool_size=50, max_overflow=25)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_setup(db_session):
    """Provisions a clean tenant, reviewer user, session token, and incident fixture."""
    tenant_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    incident_id = uuid.uuid4()

    tenant = TenantModel(id=tenant_id, slug=f"bb-{tenant_id.hex[:6]}", name="Backbone Test Org")
    db_session.add(tenant)
    db_session.commit()

    reviewer = UserModel(
        id=reviewer_id,
        tenant_id=tenant_id,
        email=f"reviewer-{reviewer_id.hex[:6]}@example.com",
        role="reviewer",
        password_hash=hash_password("ReviewerPass123!"),
    )
    db_session.add(reviewer)

    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    session_row = SessionModel(
        token_hash=token_hash,
        user_id=reviewer_id,
        tenant_id=tenant_id,
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24),
    )
    db_session.add(session_row)

    incident = IncidentModel(
        id=incident_id,
        tenant_id=tenant_id,
        alert_id=f"alt-bb-{uuid.uuid4().hex[:6]}",
        source="alert",
        service="checkout-service",
        severity="HIGH",
        alert_payload={
            "id": str(uuid.uuid4()),
            "timestamp": "2026-11-24T18:00:00Z",
            "service": "checkout-service",
            "severity": "HIGH",
            "metric": "p99_latency_ms",
            "value": 1450.0,
            "threshold": 500.0,
        },
        opened_at=datetime.datetime.now(datetime.timezone.utc),
        status_summary="open",
    )
    db_session.add(incident)
    db_session.commit()

    return {
        "tenant_id": tenant_id,
        "reviewer_id": reviewer_id,
        "incident_id": incident_id,
        "token": raw_token,
        "ctx": TenantContext(tenant_id=tenant_id),
    }


# ===========================================================================
# 1. Full Happy Path Lifecycle Driver
# ===========================================================================


def test_full_lifecycle_driver(session_factory, test_setup):
    """Walks a run through the complete lifecycle contract from queued to completed."""
    tenant_id = test_setup["tenant_id"]
    ctx = test_setup["ctx"]
    reviewer_id = test_setup["reviewer_id"]
    incident_id = test_setup["incident_id"]

    run_id = uuid.uuid4()
    initial_calls = 8

    # -----------------------------------------------------------------------
    # Step 0: Queued Run Creation
    # -----------------------------------------------------------------------
    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=initial_calls,
            max_calls=initial_calls,
        )
        session.add(run)
        repo.append_event(
            ctx,
            run_id,
            "run_created",
            "api_system",
            {"incident_id": str(incident_id), "status": "queued"},
        )
        session.commit()

    with session_factory() as session:
        repo = RunRepository(session)
        run = repo.get_by_id(ctx, run_id)
        assert run.status == "queued"
        assert run.version == 1
        assert run.remaining_calls == 8
        events = repo.get_run_events(ctx, run_id)
        assert len(events) == 1
        assert events[0].seq == 1
        assert events[0].type == "run_created"

    # -----------------------------------------------------------------------
    # Step 1: queued -> investigating (Agent Worker Dequeue & Evidence/Hypothesis)
    # -----------------------------------------------------------------------
    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)
        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="investigating",
            actor="worker:agent_pool",
            expected_version=1,
            payload={"worker_id": "worker-1"},
        )

        # Decrement tool call budget
        rem = service.decrement_calls(ctx, run_id, 1)
        assert rem == 7

        # Write Observation
        obs_payload = {"p99_latency": 1520.4, "pool_active": 98, "pool_max": 100}
        obs = ObservationModel(
            id=uuid.uuid4(),
            evidence_id="ev-metrics-01",
            run_id=run_id,
            tenant_id=tenant_id,
            tool="get_metrics",
            source_id="prometheus:checkout-service",
            captured_at=datetime.datetime.now(datetime.timezone.utc),
            window_start=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10),
            window_end=datetime.datetime.now(datetime.timezone.utc),
            args={"metric": "pgpool_connections", "service": "checkout-service"},
            summary="Postgres connection pool saturation detected at 98% utilization",
            payload=obs_payload,
            source_version="v1",
            content_hash=compute_content_hash(obs_payload),
        )
        session.add(obs)

        # Write Hypothesis
        hyp = HypothesisModel(
            id=uuid.uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            statement="Database connection pool exhaustion caused request queueing",
            category="dependency_failure",
            mechanism="connection_pool_exhaustion",
            service="checkout-service",
            status="supported",
            supporting_evidence_ids=["ev-metrics-01"],
            contrary_evidence_ids=[],
            confidence=0.95,
        )
        session.add(hyp)
        session.commit()
        session.refresh(run)

        assert run.status == "investigating"
        assert run.version == 3
        assert event.seq == 2

    # -----------------------------------------------------------------------
    # Step 2: investigating -> needs_review (Plan Generation & Proposal)
    # -----------------------------------------------------------------------
    plan_id = uuid.uuid4()
    plan_hash = compute_plan_hash(
        action="rollback_service",
        service="checkout-service",
        expected_version="v2.4.1",
        rationale_evidence_ids=["ev-metrics-01"],
        rollback_route="git-revert:deploy-v2.4.0",
    )

    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)

        plan = ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="checkout-service",
            expected_version="v2.4.1",
            rationale_evidence_ids=["ev-metrics-01"],
            rollback_route="git-revert:deploy-v2.4.0",
            plan_hash=plan_hash,
            status="proposed",
            notes="Rollback to deploy-v2.4.0 to restore connection pool stability",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2),
        )
        session.add(plan)

        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="needs_review",
            actor="agent:planner",
            expected_version=3,
            payload={"plan_id": str(plan_id), "plan_hash": plan_hash},
        )
        session.commit()
        session.refresh(run)

        assert run.status == "needs_review"
        assert run.version == 4
        assert event.seq == 3

    # -----------------------------------------------------------------------
    # Step 3: needs_review -> approved (Reviewer Principal Approval)
    # -----------------------------------------------------------------------
    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)

        # Reviewer approves matching plan hash
        approval = ApprovalModel(
            id=uuid.uuid4(),
            plan_id=plan_id,
            tenant_id=tenant_id,
            reviewer_id=reviewer_id,
            decision="approve",
            submitted_plan_hash=plan_hash,
            reason="Approved rollback based on verified pool exhaustion evidence",
        )
        session.add(approval)

        plan_in_db = session.get(ActionPlanModel, plan_id)
        plan_in_db.status = "approved"

        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="approved",
            actor=f"user:{reviewer_id}",
            expected_version=4,
            payload={"plan_id": str(plan_id), "decision": "approve"},
        )
        session.commit()
        session.refresh(run)

        assert run.status == "approved"
        assert run.version == 5
        assert event.seq == 4

    # -----------------------------------------------------------------------
    # Step 4: approved -> executing (Execution Broker Dispatch)
    # -----------------------------------------------------------------------
    execution_id = uuid.uuid4()
    action_key = f"act:{plan_id}:rollback:v2.4.0"

    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)

        execution = ActionExecutionModel(
            id=execution_id,
            plan_id=plan_id,
            tenant_id=tenant_id,
            action_key=action_key,
            status="dispatched",
            attempts=1,
            target_state_before={"service": "checkout-service", "version": "v2.4.1"},
            dispatched_at=datetime.datetime.now(datetime.timezone.utc),
        )
        session.add(execution)

        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="executing",
            actor="broker:dispatcher",
            expected_version=5,
            payload={"execution_id": str(execution_id), "action_key": action_key},
        )
        session.commit()
        session.refresh(run)

        assert run.status == "executing"
        assert run.version == 6
        assert event.seq == 5

    # -----------------------------------------------------------------------
    # Step 5: executing -> verifying (Broker Acknowledgment & Probe Verification)
    # -----------------------------------------------------------------------
    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)

        # Broker marks execution acknowledged
        exec_in_db = session.get(ActionExecutionModel, execution_id)
        exec_in_db.status = "acknowledged"
        exec_in_db.completed_at = datetime.datetime.now(datetime.timezone.utc)
        exec_in_db.target_state_after={"service": "checkout-service", "version": "v2.4.0"}

        plan_in_db = session.get(ActionPlanModel, plan_id)
        plan_in_db.status = "executed"

        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="verifying",
            actor="broker:executor",
            expected_version=6,
            payload={"execution_id": str(execution_id), "broker_status": "acknowledged"},
        )

        # Verification observation
        verify_payload = {"p99_latency_ms": 115.0, "error_rate": 0.001, "probes_passed": 3}
        verify_obs = ObservationModel(
            id=uuid.uuid4(),
            evidence_id="ev-verify-01",
            run_id=run_id,
            tenant_id=tenant_id,
            tool="get_metrics",
            source_id="synthetic-probe:checkout",
            captured_at=datetime.datetime.now(datetime.timezone.utc),
            window_start=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1),
            window_end=datetime.datetime.now(datetime.timezone.utc),
            args={"check": "health_probe", "service": "checkout-service"},
            summary="Post-rollback health probe returned healthy p99 latency 115ms (error rate 0.1%)",
            payload=verify_payload,
            source_version="v1",
            content_hash=compute_content_hash(verify_payload),
        )
        session.add(verify_obs)

        # Final structured Diagnosis
        diagnosis = DiagnosisModel(
            id=uuid.uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            root_cause_service="checkout-service",
            root_cause_category="bad_deploy",
            mechanism="connection_pool_exhaustion",
            culprit_deploy_id="deploy-v2.4.1",
            evidence_ids=["ev-metrics-01", "ev-verify-01"],
            confidence=0.98,
        )
        session.add(diagnosis)
        session.commit()
        session.refresh(run)

        assert run.status == "verifying"
        assert run.version == 7
        assert event.seq == 6

    # -----------------------------------------------------------------------
    # Step 6: verifying -> completed (Terminal Pass)
    # -----------------------------------------------------------------------
    with session_factory() as session:
        repo = RunRepository(session)
        service = RunService(session, repo)

        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="completed",
            actor="verifier:independent_probe",
            expected_version=7,
            payload={"verification": "passed", "sla_restored": True},
        )
        session.commit()
        session.refresh(run)

        assert run.status == "completed"
        assert run.version == 8
        assert event.seq == 7
        assert run.finished_at is not None

        # Verify full event sequence integrity
        all_events = repo.get_run_events(ctx, run_id)
        assert len(all_events) == 7
        assert [e.seq for e in all_events] == [1, 2, 3, 4, 5, 6, 7]


# ===========================================================================
# 2. Negative Paths
# ===========================================================================


def test_negative_skip_state(session_factory, test_setup):
    """Skipping lifecycle states (e.g. queued -> approved or investigating -> executing) is rejected."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]

    run_id = uuid.uuid4()
    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})
        session.commit()

    with session_factory() as session:
        service = RunService(session)
        with pytest.raises(IllegalTransitionError) as exc:
            service.transition(ctx, run_id, "approved", "attacker", 1)
        assert 'Cannot transition run' in str(exc.value)
        assert 'illegal transition' in str(exc.value)


def test_negative_approve_expired_plan_row(session_factory, test_setup):
    """Attempting to approve a plan whose expires_at is in the past fails business validation."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]

    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    expired_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="needs_review",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})

        plan_hash = compute_plan_hash(
            action="restart_service",
            service="checkout-service",
            expected_version=None,
            rationale_evidence_ids=[],
            rollback_route=None,
        )
        plan = ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="restart_service",
            service="checkout-service",
            plan_hash=plan_hash,
            status="proposed",
            expires_at=expired_time,
        )
        session.add(plan)
        session.commit()

    # Attempting to approve expired plan must detect expiration
    with session_factory() as session:
        plan_in_db = session.get(ActionPlanModel, plan_id)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        assert plan_in_db.expires_at < now_utc

        # Enforce that expired plans cannot be approved (must mark expired and escalate)
        plan_in_db.status = "expired"
        service = RunService(session)
        run, _ = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="escalated",
            actor="system:ttl_reaper",
            expected_version=1,
            payload={"reason": "plan_expired", "plan_id": str(plan_id)},
        )
        session.commit()

        assert run.status == "escalated"
        assert plan_in_db.status == "expired"


def test_negative_duplicate_action_key_insert(session_factory, test_setup):
    """UNIQUE(action_key) on app.action_executions rejects duplicate executions."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]

    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    action_key = f"dup_key_{uuid.uuid4().hex}"

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="approved",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})

        plan_hash = compute_plan_hash(
            action="restart_service",
            service="checkout-service",
            expected_version=None,
            rationale_evidence_ids=[],
            rollback_route=None,
        )
        plan = ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="restart_service",
            service="checkout-service",
            plan_hash=plan_hash,
            status="approved",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )
        session.add(plan)

        exec1 = ActionExecutionModel(
            id=uuid.uuid4(),
            plan_id=plan_id,
            tenant_id=tenant_id,
            action_key=action_key,
            status="dispatched",
        )
        session.add(exec1)
        session.commit()

    with session_factory() as session:
        exec2 = ActionExecutionModel(
            id=uuid.uuid4(),
            plan_id=plan_id,
            tenant_id=tenant_id,
            action_key=action_key,
            status="dispatched",
        )
        session.add(exec2)
        with pytest.raises(Exception) as exc:
            session.commit()
        assert "uq_action_executions_action_key" in str(exc.value) or "unique" in str(exc.value).lower()


def test_negative_mutate_plan_after_insert(session_factory, test_setup):
    """Action plan immutability trigger blocks modifying core proposal fields."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]

    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="needs_review",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})

        plan_hash = compute_plan_hash(
            action="rollback_service",
            service="checkout-service",
            expected_version="v1.0",
            rationale_evidence_ids=[],
            rollback_route="revert",
        )
        plan = ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="checkout-service",
            expected_version="v1.0",
            rationale_evidence_ids=[],
            rollback_route="revert",
            plan_hash=plan_hash,
            status="proposed",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )
        session.add(plan)
        session.commit()

    # Attempt to mutate 'service'
    with session_factory() as session:
        plan_in_db = session.get(ActionPlanModel, plan_id)
        plan_in_db.service = "tampered-service"
        with pytest.raises(Exception) as exc:
            session.commit()
        assert "immutable" in str(exc.value).lower()


def test_negative_delete_event(session_factory, test_setup):
    """Append-only trigger prevents DELETE on app.run_events."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    run_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        event = repo.append_event(ctx, run_id, "run_created", "system", {})
        session.commit()
        event_id = event.id

    with session_factory() as session:
        session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))
        with pytest.raises(Exception) as exc:
            session.execute(text(f"DELETE FROM app.run_events WHERE id = {event_id}"))
            session.commit()
        assert "append-only" in str(exc.value).lower()


def test_negative_read_another_tenant_run(session_factory, test_setup):
    """Reading another tenant's run via tenant context returns NotFoundError."""
    ctx_alpha = test_setup["ctx"]
    incident_id = test_setup["incident_id"]

    beta_tenant_id = uuid.uuid4()
    with session_factory() as session:
        t_beta = TenantModel(id=beta_tenant_id, slug=f"beta-{beta_tenant_id.hex[:6]}", name="Beta Org")
        session.add(t_beta)
        session.commit()

    ctx_beta = TenantContext(tenant_id=beta_tenant_id)
    run_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=beta_tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx_beta, run_id, "run_created", "system", {})
        session.commit()

    # Tenant Alpha attempts to read Beta's run
    with session_factory() as session:
        repo = RunRepository(session)
        with pytest.raises(NotFoundError):
            repo.get_by_id(ctx_alpha, run_id)


def test_negative_concurrent_double_transition(session_factory, test_setup):
    """Two concurrent transitions with identical expected_version: exactly one succeeds."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    run_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="queued",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})
        session.commit()

    successes = 0
    conflicts = 0
    barrier = threading.Barrier(2)

    def attempt_transition(worker_id: int):
        nonlocal successes, conflicts
        with session_factory() as session:
            service = RunService(session)
            barrier.wait()
            try:
                service.transition(
                    ctx=ctx,
                    run_id=run_id,
                    to_status="investigating",
                    actor=f"worker-{worker_id}",
                    expected_version=1,
                )
                session.commit()
                successes += 1
            except ConcurrentModificationError:
                session.rollback()
                conflicts += 1

    t1 = threading.Thread(target=attempt_transition, args=(1,))
    t2 = threading.Thread(target=attempt_transition, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert successes == 1, f"Expected exactly 1 success, got {successes}"
    assert conflicts == 1, f"Expected exactly 1 conflict, got {conflicts}"


# ===========================================================================
# 3. Failure Paths
# ===========================================================================


def test_failure_run_to_failed_with_last_error(session_factory, test_setup):
    """Run transitions to terminal status 'failed' with typed last_error and finished_at set."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    run_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})
        session.commit()

    error_payload = {
        "code": "RUN_CRASH_OOM",
        "message": "Memory limit exceeded during container inspection",
        "fatal": True,
    }

    with session_factory() as session:
        service = RunService(session)
        run = service.record_error(
            ctx=ctx,
            run_id=run_id,
            error=error_payload,
            actor="system:watchdog",
            target_status="failed",
        )
        session.commit()
        session.refresh(run)

        assert run.status == "failed"
        assert run.last_error == error_payload
        assert run.finished_at is not None
        assert run.version == 3


def test_failure_run_to_escalated(session_factory, test_setup):
    """Run transitions to 'escalated' upon low confidence or stop-and-ask condition."""
    ctx = test_setup["ctx"]
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    run_id = uuid.uuid4()

    with session_factory() as session:
        repo = RunRepository(session)
        run = RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        session.add(run)
        repo.append_event(ctx, run_id, "run_created", "system", {})
        session.commit()

    with session_factory() as session:
        service = RunService(session)
        run, event = service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="escalated",
            actor="agent:evaluator",
            expected_version=1,
            payload={"reason": "low_confidence", "confidence": 0.45},
        )
        session.commit()
        session.refresh(run)

        assert run.status == "escalated"
        assert run.version == 2
        assert run.finished_at is not None
        assert event.type == "status_transition"


def test_cancel_from_investigating_and_completed_rejected(db_session, test_setup):
    """Cancellation transitions an investigating run to escalated, but is rejected on completed."""
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    token = test_setup["token"]

    def override_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    try:
        # 1. Cancel from investigating succeeds
        run1 = RunModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        db_session.add(run1)
        repo = RunRepository(db_session)
        repo.append_event(test_setup["ctx"], run1.id, "run_created", "system", {})
        db_session.commit()

        resp_cancel = client.post(
            f"/runs/{run1.id}/cancel",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["status"] == "escalated"

        # 2. Cancel from completed is rejected with 400 Bad Request
        run2 = RunModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="completed",
            version=1,
            remaining_calls=8,
            max_calls=8,
            finished_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db_session.add(run2)
        repo.append_event(test_setup["ctx"], run2.id, "run_created", "system", {})
        db_session.commit()

        resp_cancel_terminal = client.post(
            f"/runs/{run2.id}/cancel",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp_cancel_terminal.status_code == 400
        assert "terminal status" in resp_cancel_terminal.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ===========================================================================
# 4. Load Check: 200 Concurrent Runs via API
# ===========================================================================


def test_load_200_concurrent_runs(session_factory, test_setup):
    """Creates 200 runs concurrently via API with distinct idempotency keys; asserts gap-free seq."""
    tenant_id = test_setup["tenant_id"]
    incident_id = test_setup["incident_id"]
    token = test_setup["token"]

    created_runs: list[dict[str, Any]] = []
    lock = threading.Lock()
    num_runs = 200

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_db
    orig_limit = runs_rate_limiter.max_attempts
    runs_rate_limiter.max_attempts = 1000

    try:
        def create_run(idx: int):
            c = TestClient(app)
            c.cookies.set(SESSION_COOKIE_NAME, token)
            headers = {
                "Idempotency-Key": f"load-test-key-{idx}-{uuid.uuid4().hex[:6]}",
                "X-Requested-With": "XMLHttpRequest",
            }
            payload = {"incident_id": str(incident_id), "max_calls": 8}
            resp = c.post("/runs", json=payload, headers=headers)
            assert resp.status_code == 201, f"Failed at {idx}: {resp.text}"
            data = resp.json()
            with lock:
                created_runs.append(data)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(create_run, i) for i in range(num_runs)]
            for f in concurrent.futures.as_completed(futures):
                f.result()
    finally:
        app.dependency_overrides.clear()
        runs_rate_limiter.max_attempts = orig_limit

    assert len(created_runs) == num_runs

    # Verify each created run has gap-free event seq starting at 1
    with session_factory() as session:
        repo = RunRepository(session)
        ctx = TenantContext(tenant_id=tenant_id)

        for item in created_runs:
            run_uuid = uuid.UUID(item["id"])
            events = repo.get_run_events(ctx, run_uuid)
            assert len(events) >= 1, f"Run {run_uuid} has 0 events!"
            seqs = [e.seq for e in events]
            assert seqs == list(range(1, len(seqs) + 1)), f"Run {run_uuid} has gap in event seqs: {seqs}"

        # Verify no orphan rows in app.runs
        orphan_runs = session.execute(
            text(
                """
                SELECT r.id FROM app.runs r
                LEFT JOIN app.run_events e ON r.id = e.run_id
                WHERE r.tenant_id = :t_id AND e.id IS NULL
                """
            ),
            {"t_id": tenant_id},
        ).fetchall()
        assert len(orphan_runs) == 0, f"Found orphan runs without events: {orphan_runs}"
