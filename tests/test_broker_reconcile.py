"""Table-driven reconciliation and guard tests against FakeTarget (Feature 9)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.models.tables import ActionExecutionModel, RunModel
from broker.main import BrokerService
from broker.target_client import FakeTarget
from shared.plans import compute_action_key, plan_hash
from shared.targets import DEFAULT_LAB_TARGETS_PATH, load_target_registry

PG_URL = os.environ.get(
    "TEST_DB_URL",
    "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:54333/opspilot",
)
PSYCOPG_URL = PG_URL.replace("postgresql+psycopg://", "postgresql://")


def _pg_available() -> bool:
    try:
        eng = create_engine(PG_URL, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")


def _drain_active(cur: psycopg.Cursor) -> None:
    cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
    cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE status = 'dispatched'")
    cur.execute(
        "UPDATE app.action_executions SET status = 'failed' WHERE status IN ('claimed', 'uncertain')"
    )


def _seed_exec(conn: psycopg.Connection) -> tuple[UUID, UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    user_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    exec_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=900)
    ev_id = f"ev_{uuid4().hex[:8]}"

    p_hash = plan_hash(
        run_id=run_id,
        tenant_id=tenant_id,
        action="rollback_service",
        service="orders",
        target_id="lab-orders",
        expected_version="v1.3.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=[ev_id],
        expires_at=expires_at,
    )
    a_key = compute_action_key(plan_id, p_hash)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (tenant_id, "Reconcile Test", f"rt-{tenant_id.hex[:8]}"),
        )
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'reviewer', 'hash')",
            (user_id, tenant_id, f"rev-{user_id.hex[:6]}@example.com"),
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, %s, 'alert', 'orders', 'HIGH', %s, now(), 'open')",
            (incident_id, tenant_id, f"alt-{uuid4().hex[:6]}", '{"alert_id":"a"}'),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version) VALUES (%s, %s, %s, 'approved', 1)",
            (run_id, tenant_id, incident_id),
        )
        cur.execute(
            """
            INSERT INTO app.action_plans (
                id, run_id, tenant_id, action, service, target_id, expected_version,
                rollback_to_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at
            ) VALUES (%s, %s, %s, 'rollback_service', 'orders', 'lab-orders', 'v1.3.0', 'v1.0.0', %s, 'Re-deploy', %s, 'approved', %s)
            """,
            (plan_id, run_id, tenant_id, [ev_id], p_hash, expires_at),
        )
        cur.execute(
            """
            INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, reason)
            VALUES (%s, %s, %s, %s, 'approve', %s, 'ok')
            """,
            (uuid4(), plan_id, tenant_id, user_id, p_hash),
        )
        cur.execute(
            """
            INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, target_id, status, attempts)
            VALUES (%s, %s, %s, %s, 'lab-orders', 'queued', 0)
            """,
            (exec_id, plan_id, tenant_id, a_key),
        )
    return tenant_id, run_id, plan_id, exec_id, a_key


def _events(conn: psycopg.Connection, exec_id: UUID) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seq, type FROM app.execution_events WHERE execution_id = %s ORDER BY seq ASC",
            (exec_id,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


@pytest.mark.parametrize(
    "case_name",
    [
        "dropped_response_after_apply",
        "fail_before_apply",
        "applied_by_something_else",
        "target_down_whole_deadline",
        "duplicate_delivery",
    ],
)
def test_reconcile_table_driven(case_name: str):
    """Table-driven reconcile tests against FakeTarget failure modes."""
    eng = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=eng, future=True)
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)

    try:
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                _drain_active(cur)
            t_id, r_id, p_id, e_id, a_key = _seed_exec(conn)

            svc = BrokerService(
                SessionLocal,
                target,
                registry,
                max_attempts=3,
                retry_backoff_seconds=0.01,
                reconcile_deadline_seconds=0.15,
            )

            if case_name == "dropped_response_after_apply":
                target.hooks.drop_response_after_apply = True
                assert svc.process_next() == "uncertain"
                assert target.call_count == 1
                target.hooks.drop_response_after_apply = False

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                        (e_id,),
                    )
                assert svc.process_next() == "reconciled_applied"
                assert target.call_count == 1  # ZERO second dispatch
                assert list(target.applied_actions.keys()) == [a_key]

            elif case_name == "fail_before_apply":
                target.hooks.fail_before_apply = True
                assert svc.process_next() == "uncertain"
                assert target.call_count == 1
                target.hooks.fail_before_apply = False

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                        (e_id,),
                    )
                assert svc.process_next() == "applied"
                assert target.call_count == 2  # retried once with same action_key
                assert list(target.applied_actions.keys()) == [a_key]

            elif case_name == "applied_by_something_else":
                target.hooks.fail_before_apply = True
                assert svc.process_next() == "uncertain"
                target.hooks.fail_before_apply = False
                target.versions["lab-orders"] = "v1.1.0"  # changed without action_key

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                        (e_id,),
                    )
                assert svc.process_next() == "escalated_unknown"
                assert list(target.applied_actions.keys()) == []
                with SessionLocal() as s:
                    s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t_id)})
                    rn = s.get(RunModel, r_id)
                    ex = s.get(ActionExecutionModel, e_id)
                    assert ex is not None and ex.status == "failed"
                    assert rn is not None and rn.status == "escalated"

            elif case_name == "target_down_whole_deadline":
                target.hooks.timeout_on_call = True
                assert svc.process_next() == "uncertain"
                target.unavailable = True

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                        (e_id,),
                    )
                assert svc.process_next() == "escalated_unknown"
                assert list(target.applied_actions.keys()) == []

            elif case_name == "duplicate_delivery":
                # First delivery succeeds via drop_response -> uncertain -> reconciled_applied,
                # or simulated duplicate claim after dispatched -> reconciled_applied with 1 effect
                target.hooks.drop_response_after_apply = True
                assert svc.process_next() == "uncertain"
                target.hooks.drop_response_after_apply = False
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                        (e_id,),
                    )
                svc2 = BrokerService(SessionLocal, target, registry, broker_id="broker-2")
                assert svc2.process_next() == "reconciled_applied"
                assert list(target.applied_actions.keys()) == [a_key]
                assert len(target.applied_actions) == 1

            ev_list = _events(conn, e_id)
            print(f"\n--- RECONCILE CASE: {case_name} ---")
            print("events:", " -> ".join(f"{s}:{t}" for s, t in ev_list))
            print("target_applied_actions:", list(target.applied_actions.keys()))
    finally:
        eng.dispose()


def test_guard_forbids_redispatch_without_reconcile_record():
    """Guard forbids re-dispatching an uncertain execution without a prior not_applied record."""
    eng = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=eng, future=True)
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)

    try:
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                _drain_active(cur)
            _, _, _, e_id, _ = _seed_exec(conn)
            target.hooks.timeout_on_call = True
            svc = BrokerService(SessionLocal, target, registry)
            assert svc.process_next() == "uncertain"
            calls_before = target.call_count

            with SessionLocal() as s:
                with pytest.raises(RuntimeError, match="Guard violation"):
                    svc._assert_reconciled_not_applied(s, e_id)
            assert target.call_count == calls_before
    finally:
        eng.dispose()
