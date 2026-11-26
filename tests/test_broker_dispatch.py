"""Real-PostgreSQL + FakeTarget tests for Broker dispatch path (Feature 9)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.models.tables import ActionExecutionModel, ActionPlanModel, RunModel
from broker.main import BrokerService
from broker.target_client import FakeTarget, LabAdminClient
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


def _seed_approved_execution(
    conn: psycopg.Connection,
    *,
    action: str = "rollback_service",
    service: str = "orders",
    target_id: str = "lab-orders",
    expected_version: str = "v1.3.0",
    rollback_to_version: str | None = "v1.0.0",
) -> tuple[UUID, UUID, UUID, UUID, str]:
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
        action=action,
        service=service,
        target_id=target_id,
        expected_version=expected_version,
        rollback_to_version=rollback_to_version,
        rationale_evidence_ids=[ev_id],
        expires_at=expires_at,
    )
    a_key = compute_action_key(plan_id, p_hash)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (tenant_id, "Dispatch Test", f"dt-{tenant_id.hex[:8]}"),
        )
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'reviewer', 'hash')",
            (user_id, tenant_id, f"rev-{user_id.hex[:6]}@example.com"),
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, %s, 'alert', %s, 'HIGH', %s, now(), 'open')",
            (incident_id, tenant_id, f"alt-{uuid4().hex[:6]}", service, '{"alert_id":"a"}'),
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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Re-deploy', %s, 'approved', %s)
            """,
            (
                plan_id,
                run_id,
                tenant_id,
                action,
                service,
                target_id,
                expected_version,
                rollback_to_version,
                [ev_id],
                p_hash,
                expires_at,
            ),
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
            VALUES (%s, %s, %s, %s, %s, 'queued', 0)
            """,
            (exec_id, plan_id, tenant_id, a_key, target_id),
        )
    return tenant_id, run_id, plan_id, exec_id, a_key


def _fetch_events(conn: psycopg.Connection, exec_id: UUID) -> list[tuple[int, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seq, type, COALESCE(payload::text, '{}') FROM app.execution_events WHERE execution_id = %s ORDER BY seq ASC",
            (exec_id,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def test_dispatch_applied_already_applied_precondition_4xx_uncertain_and_crash():
    """Tests full dispatch mapping, idempotent resume, and prints execution event tables."""
    eng = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=eng, future=True)
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    secret_token = "super-secret-admin-token-never-logged"

    try:
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                _drain_active(cur)

            # 1. APPLIED
            t1, r1, p1, e1, k1 = _seed_approved_execution(conn)
            target1 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc1 = BrokerService(SessionLocal, target1, registry)
            assert svc1.process_next() == "applied"
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t1)})
                ex1 = s.get(ActionExecutionModel, e1)
                pl1 = s.get(ActionPlanModel, p1)
                rn1 = s.get(RunModel, r1)
                assert ex1 is not None and ex1.status == "acknowledged"
                assert ex1.target_state_before is not None and ex1.target_state_before["current_version"] == "v1.3.0"
                assert ex1.target_state_after is not None and ex1.target_state_after["current_version"] == "v1.0.0"
                assert pl1 is not None and pl1.status == "executed"
                assert rn1 is not None and rn1.status == "verifying"

            ev_applied = _fetch_events(conn, e1)
            assert [e[1] for e in ev_applied] == ["execution_claimed", "execution_dispatched", "execution_acknowledged"]

            # 2. ALREADY_APPLIED
            t2, r2, p2, e2, k2 = _seed_approved_execution(conn)
            target2 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            # Pre-seed applied_actions in FakeTarget to simulate prior execution
            target2.apply_rollback("lab-orders", k2, "v1.3.0", "v1.0.0")
            target2.versions["lab-orders"] = "v1.3.0"  # keep B8 passing so dispatch calls target
            svc2 = BrokerService(SessionLocal, target2, registry)
            assert svc2.process_next() == "already_applied"
            ev_already = _fetch_events(conn, e2)
            assert [e[1] for e in ev_already] == [
                "execution_claimed",
                "execution_dispatched",
                "execution_acknowledged",
                "already_applied",
            ]

            # 3. PRECONDITION_FAILED (version changes between B8 and apply_rollback)
            class RaceDriftTarget(FakeTarget):
                def apply_rollback(self, target_id, action_key, expected_current_version, to_version):
                    self.versions[target_id] = "v1.1.0"
                    return super().apply_rollback(target_id, action_key, expected_current_version, to_version)

            t3, r3, p3, e3, _ = _seed_approved_execution(conn)
            target3 = RaceDriftTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc3 = BrokerService(SessionLocal, target3, registry)
            assert svc3.process_next() == "precondition_failed"
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t3)})
                ex3 = s.get(ActionExecutionModel, e3)
                pl3 = s.get(ActionPlanModel, p3)
                rn3 = s.get(RunModel, r3)
                assert ex3 is not None and ex3.status == "failed"
                assert ex3.last_error is not None and ex3.last_error["code"] == "target_changed"
                assert pl3 is not None and pl3.status == "superseded"
                assert rn3 is not None and rn3.status == "escalated"
            ev_precond = _fetch_events(conn, e3)
            assert [e[1] for e in ev_precond] == ["execution_claimed", "execution_dispatched", "execution_failed"]

            # 4. 4xx -> failed, run -> escalated
            t4, r4, p4, e4, _ = _seed_approved_execution(conn)
            target4 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            target4.hooks.http_4xx_error = True
            svc4 = BrokerService(SessionLocal, target4, registry)
            assert svc4.process_next() == "failed"
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t4)})
                ex4 = s.get(ActionExecutionModel, e4)
                rn4 = s.get(RunModel, r4)
                assert ex4 is not None and ex4.status == "failed"
                assert ex4.last_error is not None and ex4.last_error["code"] == "target_client_error"
                assert rn4 is not None and rn4.status == "escalated"

            # 5. Timeout -> uncertain (run stays executing)
            t5, r5, _, e5, _ = _seed_approved_execution(conn)
            target5 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            target5.hooks.timeout_on_call = True
            svc5 = BrokerService(SessionLocal, target5, registry)
            assert svc5.process_next() == "uncertain"
            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t5)})
                ex5 = s.get(ActionExecutionModel, e5)
                rn5 = s.get(RunModel, r5)
                assert ex5 is not None and ex5.status == "uncertain"
                assert rn5 is not None and rn5.status == "executing"

            # 6. Crash after dispatched commit -> uncertain on restart (no re-dispatch)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.action_executions SET status = 'failed' WHERE id = %s", (e5,)
                )
            _, _, _, e6, _ = _seed_approved_execution(conn)
            with conn.cursor() as cur:
                cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE id = %s", (e6,))
                cur.execute(
                    "UPDATE app.action_executions SET status = 'dispatched', lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                    (e6,),
                )
            target6 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc6 = BrokerService(SessionLocal, target6, registry)
            assert svc6.process_next() == "uncertain"
            assert target6.call_count == 0
            with conn.cursor() as cur:
                cur.execute("UPDATE app.action_executions SET status = 'failed' WHERE id = %s", (e6,))

            # 7. DB-level inflight uniqueness per target (dispatched/uncertain)
            _, _, _, e7a, _ = _seed_approved_execution(conn, target_id="lab-inventory", service="inventory", action="restart_service", expected_version="v1.0.0", rollback_to_version=None)
            _, _, _, e7b, _ = _seed_approved_execution(conn, target_id="lab-inventory", service="inventory", action="restart_service", expected_version="v1.0.0", rollback_to_version=None)
            with conn.cursor() as cur:
                cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE id IN (%s, %s)", (e7a, e7b))
                cur.execute("UPDATE app.action_executions SET status = 'dispatched' WHERE id = %s", (e7a,))
                with pytest.raises(psycopg.errors.UniqueViolation):
                    cur.execute("UPDATE app.action_executions SET status = 'dispatched' WHERE id = %s", (e7b,))
                cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE id = %s", (e7a,))
                cur.execute("UPDATE app.action_executions SET status = 'failed' WHERE id IN (%s, %s)", (e7a, e7b))

            # 8. No token in stored rows
            client = LabAdminClient(registry=registry, admin_token=SecretStr(secret_token))
            assert client._resolve_base_url("lab-orders") == "http://orders:8000"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.action_executions WHERE COALESCE(target_response::text, '') LIKE %s OR COALESCE(last_error::text, '') LIKE %s",
                    (f"%{secret_token}%", f"%{secret_token}%"),
                )
                assert cur.fetchone()[0] == 0

        for title, rows in [
            ("APPLIED", ev_applied),
            ("ALREADY_APPLIED", ev_already),
            ("PRECONDITION_FAILED", ev_precond),
        ]:
            print(f"\n--- EXECUTION EVENTS: {title} ---")
            for seq, etype, payload in rows:
                print(f"seq={seq} | type={etype:<24} | payload={payload}")
    finally:
        eng.dispose()
