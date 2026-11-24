"""Tests for RunRepository and RunService.

Verifies:
1. Two concurrent transitions with the same expected_version: exactly one succeeds.
2. Event seq is gap-free and monotonic under concurrency.
3. Illegal transitions rejected in application layer (fail-fast) and database trigger.
4. decrement_calls never goes negative under 50 concurrent calls.
5. Error recording and conditional status transition.
"""

from __future__ import annotations

import concurrent.futures
import threading
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    BudgetExhaustedError,
    ConcurrentModificationError,
    IllegalTransitionError,
)
from apps.api.models.tables import IncidentModel, RunEventModel, RunModel, TenantModel
from apps.api.repositories.run_repository import RunRepository
from apps.api.services.run_service import RunService

DB_URL = "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DB_URL, pool_size=60, max_overflow=20)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def tenant_context(session_factory):
    tenant_id = uuid.uuid4()
    with session_factory() as session:
        t = TenantModel(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="Test Tenant")
        session.add(t)
        session.commit()
    return TenantContext(tenant_id=tenant_id)


def create_test_incident_and_run(
    session_factory,
    ctx: TenantContext,
    status: str = "queued",
    remaining_calls: int = 10,
    max_calls: int = 10,
    with_event: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper to create a fresh incident and run inside tenant scope."""
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    with session_factory() as session:
        inc = IncidentModel(
            id=inc_id,
            tenant_id=ctx.tenant_id,
            alert_id=f"alert-{inc_id.hex[:6]}",
            source="alert",
            service="orders",
            severity="HIGH",
            alert_payload={"alert": "latency_high"},
            opened_at=session.execute(select(IncidentModel.created_at)).scalar() or uuid.uuid4(),
            status_summary="open",
        )
        session.add(inc)

        run = RunModel(
            id=run_id,
            tenant_id=ctx.tenant_id,
            incident_id=inc_id,
            status=status,
            version=1,
            remaining_calls=remaining_calls,
            max_calls=max_calls,
        )
        session.add(run)
        if with_event:
            evt = RunEventModel(
                run_id=run_id,
                tenant_id=ctx.tenant_id,
                seq=1,
                type="run_created",
                actor="system",
                payload={},
            )
            session.add(evt)
        session.commit()
    return inc_id, run_id


def test_concurrent_transitions_optimistic_locking(session_factory, tenant_context):
    """Verifies that two concurrent transitions with same expected_version: exactly one succeeds."""
    _, run_id = create_test_incident_and_run(session_factory, tenant_context, status="queued")

    success_count = 0
    conflict_count = 0
    barrier = threading.Barrier(2)

    def attempt_transition(actor: str) -> None:
        nonlocal success_count, conflict_count
        with session_factory() as session:
            service = RunService(session)
            barrier.wait()  # synchronize threads to strike concurrently
            try:
                service.transition(
                    ctx=tenant_context,
                    run_id=run_id,
                    to_status="investigating",
                    actor=actor,
                    expected_version=1,
                    payload={"actor": actor},
                )
                session.commit()
                success_count += 1
            except ConcurrentModificationError:
                session.rollback()
                conflict_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(attempt_transition, "worker-1")
        f2 = executor.submit(attempt_transition, "worker-2")
        f1.result()
        f2.result()

    assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
    assert conflict_count == 1, f"Expected exactly 1 conflict, got {conflict_count}"

    # Verify final state in DB
    with session_factory() as session:
        service = RunService(session)
        run = service.run_repo.get_by_id(tenant_context, run_id)
        assert run.status == "investigating"
        assert run.version == 2


def test_monotonic_gap_free_event_seq_under_concurrency(session_factory, tenant_context):
    """Verifies that event seq numbers are strictly monotonic and gap-free under concurrency."""
    _, run_id = create_test_incident_and_run(
        session_factory, tenant_context, status="investigating", with_event=False
    )

    total_events = 25
    barrier = threading.Barrier(total_events)

    def append_event(i: int) -> int:
        with session_factory() as session:
            service = RunService(session)
            barrier.wait()
            evt = service.run_repo.append_event(
                ctx=tenant_context,
                run_id=run_id,
                event_type="test_event",
                actor=f"worker-{i}",
                payload={"index": i},
            )
            session.commit()
            return evt.seq

    with concurrent.futures.ThreadPoolExecutor(max_workers=total_events) as executor:
        futures = [executor.submit(append_event, i) for i in range(total_events)]
        seqs = [f.result() for f in futures]

    assert len(seqs) == total_events
    # Check that sequence numbers are exactly 1 .. total_events with no duplicates or gaps
    sorted_seqs = sorted(seqs)
    expected_seqs = list(range(1, total_events + 1))
    assert sorted_seqs == expected_seqs, f"Expected {expected_seqs}, got {sorted_seqs}"


def test_illegal_transition_rejected_in_app_and_db(session_factory, tenant_context):
    """Verifies that illegal transitions are rejected fast by RunService and by DB trigger."""
    _, run_id = create_test_incident_and_run(session_factory, tenant_context, status="queued")

    # 1. Application-level check raises IllegalTransitionError
    with session_factory() as session:
        service = RunService(session)
        with pytest.raises(IllegalTransitionError) as exc:
            service.transition(
                ctx=tenant_context,
                run_id=run_id,
                to_status="completed",  # queued -> completed is illegal
                actor="test",
                expected_version=1,
            )
        assert "Cannot transition run" in str(exc.value)
        assert exc.value.from_status == "queued"
        assert exc.value.to_status == "completed"

    # 2. Raw repository / DB-level check (trigger backstop)
    with session_factory() as session:
        repo = RunRepository(session)
        with pytest.raises(Exception) as exc_db:
            repo.update_status_optimistic(
                ctx=tenant_context,
                run_id=run_id,
                expected_version=1,
                to_status="completed",
            )
        assert "Illegal run status transition" in str(exc_db.value)


def test_decrement_calls_never_goes_negative_under_50_concurrent_calls(
    session_factory, tenant_context
):
    """Verifies atomic decrement of remaining_calls never goes negative under 50 concurrent calls."""
    initial_budget = 20
    concurrent_requests = 50
    _, run_id = create_test_incident_and_run(
        session_factory,
        tenant_context,
        status="investigating",
        remaining_calls=initial_budget,
        max_calls=initial_budget,
    )

    success_count = 0
    exhausted_count = 0
    barrier = threading.Barrier(concurrent_requests)

    def call_tool(i: int) -> None:
        nonlocal success_count, exhausted_count
        with session_factory() as session:
            service = RunService(session)
            barrier.wait()
            try:
                service.decrement_calls(ctx=tenant_context, run_id=run_id, n=1)
                session.commit()
                success_count += 1
            except BudgetExhaustedError:
                session.rollback()
                exhausted_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_requests) as executor:
        futures = [executor.submit(call_tool, i) for i in range(concurrent_requests)]
        for f in futures:
            f.result()

    assert success_count == initial_budget, (
        f"Expected exactly {initial_budget} successful decrements, got {success_count}"
    )
    assert exhausted_count == (concurrent_requests - initial_budget), (
        f"Expected {concurrent_requests - initial_budget} exhausted errors, got {exhausted_count}"
    )

    # Verify final remaining_calls in DB is exactly 0
    with session_factory() as session:
        service = RunService(session)
        run = service.run_repo.get_by_id(tenant_context, run_id)
        assert run.remaining_calls == 0


def test_record_error_and_conditional_transition(session_factory, tenant_context):
    """Verifies record_error writes last_error and transitions only if allowed."""
    _, run_id = create_test_incident_and_run(
        session_factory, tenant_context, status="investigating", with_event=False
    )

    # 1. Record error with allowed transition: investigating -> escalated
    with session_factory() as session:
        service = RunService(session)
        run = service.record_error(
            ctx=tenant_context,
            run_id=run_id,
            error={"type": "ToolTimeout", "message": "Prometheus timed out"},
            actor="agent",
            target_status="escalated",
        )
        session.commit()
        assert run.status == "escalated"
        assert run.last_error == {"type": "ToolTimeout", "message": "Prometheus timed out"}

    # 2. Record error from escalated requesting illegal target_status ('completed')
    # Should record error without changing status away from escalated
    with session_factory() as session:
        service = RunService(session)
        run = service.record_error(
            ctx=tenant_context,
            run_id=run_id,
            error={"type": "SecondaryError", "message": "Ignored transition"},
            actor="agent",
            target_status="completed",
        )
        session.commit()
        assert (
            run.status == "escalated"
        )  # Remains in escalated because escalated -> completed is disallowable
        assert run.last_error == {"type": "SecondaryError", "message": "Ignored transition"}
