"""Real-PostgreSQL tests for Broker DB schema, transitions, roles, fencing, and events."""

from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.domain.errors import LeaseLostError
from apps.api.models.enums import ExecutionStatus
from apps.api.repositories.execution_repository import ExecutionRepository
from shared.plans import compute_action_key, plan_hash

PG_URL = os.environ.get(
    "TEST_DB_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:54333/opspilot",
    ),
)
if PG_URL.startswith("postgresql://"):
    PG_URL = "postgresql+psycopg://" + PG_URL[len("postgresql://") :]

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


@pytest.fixture(scope="module")
def pg_engine():
    eng = create_engine(PG_URL, future=True)
    yield eng
    eng.dispose()


def _seed_execution(
    conn: psycopg.Connection,
    *,
    status: str = "queued",
    lease_expires_at: datetime | None = None,
    lease_token: UUID | None = None,
) -> tuple[UUID, UUID, UUID, UUID, UUID, str]:
    """Seeds tenant, user, incident, run(approved), plan(approved), approval, execution."""
    tenant_id = uuid4()
    user_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    exec_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=900)
    ev_id = f"ev_{uuid4().hex[:8]}"

    p_hash = plan_hash(
        run_id=run_id,
        tenant_id=tenant_id,
        action="restart_service",
        service="inventory",
        target_id="lab-inventory",
        expected_version="v1.0.0",
        rollback_to_version=None,
        rationale_evidence_ids=[ev_id],
        expires_at=expires_at,
    )
    a_key = compute_action_key(plan_id, p_hash)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (tenant_id, "Broker Test", f"bt-{tenant_id.hex[:8]}"),
        )
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'reviewer', 'hash')",
            (user_id, tenant_id, f"rev-{user_id.hex[:6]}@example.com"),
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, %s, 'alert', 'inventory', 'HIGH', %s, now(), 'open')",
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
            ) VALUES (%s, %s, %s, 'restart_service', 'inventory', 'lab-inventory', 'v1.0.0', NULL, %s, %s, %s, 'approved', %s)
            """,
            (plan_id, run_id, tenant_id, [ev_id], "None", p_hash, expires_at),
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
            INSERT INTO app.action_executions (
                id, plan_id, tenant_id, action_key, status, attempts, lease_token, lease_expires_at
            ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
            """,
            (exec_id, plan_id, tenant_id, a_key, status, lease_token, lease_expires_at),
        )
    return tenant_id, user_id, run_id, plan_id, exec_id, a_key


def test_execution_transitions_table_all_legal_and_illegal():
    """Tests every legal and illegal (from_status, to_status) pair against app.execution_transitions."""
    all_statuses = [s.value for s in ExecutionStatus]
    expected_legal = {
        ("queued", "claimed"),
        ("claimed", "dispatched"),
        ("claimed", "refused"),
        ("claimed", "failed"),
        ("dispatched", "acknowledged"),
        ("dispatched", "refused"),
        ("dispatched", "uncertain"),
        ("acknowledged", "succeeded"),
        ("uncertain", "reconciled"),
        ("uncertain", "dispatched"),
        ("uncertain", "failed"),
        ("reconciled", "succeeded"),
    }

    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT from_status, to_status FROM app.execution_transitions")
            db_transitions = {(r[0], r[1]) for r in cur.fetchall()}
            assert db_transitions == expected_legal

        for from_s in all_statuses:
            for to_s in all_statuses:
                if from_s == to_s:
                    continue
                _, _, _, _, exec_id, _ = _seed_execution(conn, status=from_s)
                with conn.cursor() as cur:
                    if (from_s, to_s) in expected_legal:
                        cur.execute(
                            "UPDATE app.action_executions SET status = %s WHERE id = %s",
                            (to_s, exec_id),
                        )
                        cur.execute(
                            "SELECT status FROM app.action_executions WHERE id = %s",
                            (exec_id,),
                        )
                        assert cur.fetchone()[0] == to_s
                    else:
                        with pytest.raises(psycopg.errors.CheckViolation) as exc:
                            cur.execute(
                                "UPDATE app.action_executions SET status = %s WHERE id = %s",
                                (to_s, exec_id),
                            )
                        assert "Illegal execution status transition" in str(exc.value)


def test_unique_action_key_enforced():
    """UNIQUE(action_key) prevents duplicate execution rows for the same action_key."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        tenant_id, _, run_id, _, _, a_key = _seed_execution(conn)
        plan2_id = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=900)
        p_hash2 = plan_hash(
            run_id=run_id,
            tenant_id=tenant_id,
            action="restart_service",
            service="orders",
            target_id="lab-orders",
            expected_version="v1.0.0",
            rollback_to_version=None,
            rationale_evidence_ids=["ev_2"],
            expires_at=expires_at,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.action_plans (
                    id, run_id, tenant_id, action, service, target_id, expected_version,
                    rollback_to_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at
                ) VALUES (%s, %s, %s, 'restart_service', 'orders', 'lab-orders', 'v1.0.0', NULL, %s, 'None', %s, 'approved', %s)
                """,
                (plan2_id, run_id, tenant_id, ["ev_2"], p_hash2, expires_at),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, status) VALUES (%s, %s, %s, %s, 'queued')",
                    (uuid4(), plan2_id, tenant_id, a_key),
                )


def test_broker_role_permissions_and_isolation():
    """Verifies opspilot_broker least-privilege grants, RLS, and API/worker restrictions."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        t1, _, run1, plan1, exec1, _ = _seed_execution(conn)
        t2, _, _, _, exec2, _ = _seed_execution(conn)

        with conn.cursor() as cur:
            # 1. opspilot_broker can see queued rows across ALL tenants
            cur.execute("SET ROLE opspilot_broker")
            cur.execute(
                "SELECT id FROM app.action_executions WHERE id IN (%s, %s)",
                (exec1, exec2),
            )
            found = {r[0] for r in cur.fetchall()}
            assert found == {exec1, exec2}

            # 2. opspilot_broker can read allowed tables/columns when scoped to the execution's tenant_id
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))
            cur.execute("SELECT id, plan_hash FROM app.action_plans WHERE id = %s", (plan1,))
            assert cur.fetchone() is not None
            cur.execute("SELECT decision FROM app.approvals WHERE plan_id = %s", (plan1,))
            assert cur.fetchone() is not None
            cur.execute("SELECT id, tenant_id, status, version FROM app.runs WHERE id = %s", (run1,))
            assert cur.fetchone() is not None
            cur.execute("SELECT id, tenant_id, role FROM app.users WHERE tenant_id = %s", (t1,))
            assert cur.fetchone() is not None

            # 3. opspilot_broker CANNOT read forbidden tables
            for forbidden in (
                "SELECT count(*) FROM app.observations",
                "SELECT count(*) FROM app.hypotheses",
                "SELECT count(*) FROM app.sessions",
                "SELECT count(*) FROM app.incidents",
                "SELECT count(*) FROM checkpoints.checkpoints",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(forbidden)

            # 4. opspilot_broker CANNOT INSERT or DELETE action_executions
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM app.action_executions WHERE id = %s", (exec1,))

            # 5. opspilot_broker CANNOT mutate tenant_id on action_executions
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "UPDATE app.action_executions SET tenant_id = %s WHERE id = %s",
                    (t2, exec1),
                )
            cur.execute("RESET ROLE")

            # 6. opspilot_api and opspilot_worker CANNOT UPDATE or DELETE action_executions
            for app_role in ("opspilot_api", "opspilot_worker"):
                cur.execute(f"SET ROLE {app_role}")
                cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        "UPDATE app.action_executions SET status = 'claimed' WHERE id = %s",
                        (exec1,),
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("DELETE FROM app.action_executions WHERE id = %s", (exec1,))
                cur.execute("RESET ROLE")
                cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_lease_fencing_and_reclaim(pg_engine):
    """Stale lease_token writes raise LeaseLostError; expired leases are reclaimed."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
            cur.execute("UPDATE app.action_executions SET lease_expires_at = clock_timestamp() + interval '1 hour' WHERE status IN ('claimed', 'dispatched', 'uncertain')")
        _, _, _, _, exec_id, _ = _seed_execution(conn)

    SessionLocal = sessionmaker(bind=pg_engine, future=True)
    with SessionLocal() as s1:
        s1.execute(text("SET LOCAL ROLE opspilot_broker"))
        repo1 = ExecutionRepository(s1)
        claimed = repo1.claim_next(lease_owner="broker-1", lease_seconds=30)
        assert claimed is not None
        assert claimed.id == exec_id
        assert claimed.status == "claimed"
        old_token = claimed.lease_token
        assert old_token is not None
        s1.commit()

    # Stale/wrong token write rejected
    with SessionLocal() as s_bad:
        s_bad.execute(text("SET LOCAL ROLE opspilot_broker"))
        repo_bad = ExecutionRepository(s_bad)
        with pytest.raises(LeaseLostError):
            repo_bad.mark_dispatched(exec_id, uuid4())
        s_bad.rollback()

    # Force lease expiration and reclaim by broker-2
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.action_executions SET lease_expires_at = clock_timestamp() - interval '5 seconds' WHERE id = %s",
                (exec_id,),
            )

    with SessionLocal() as s2:
        s2.execute(text("SET LOCAL ROLE opspilot_broker"))
        repo2 = ExecutionRepository(s2)
        reclaimed = repo2.claim_next(lease_owner="broker-2", lease_seconds=30)
        assert reclaimed is not None
        assert reclaimed.id == exec_id
        assert reclaimed.lease_owner == "broker-2"
        assert reclaimed.lease_token != old_token
        s2.commit()

    # Zombie broker-1 with old_token is fenced out
    with SessionLocal() as s_zombie:
        s_zombie.execute(text("SET LOCAL ROLE opspilot_broker"))
        repo_z = ExecutionRepository(s_zombie)
        with pytest.raises(LeaseLostError):
            repo_z.mark_dispatched(exec_id, old_token)
        s_zombie.rollback()


def test_concurrent_claim_disjoint_rows(pg_engine):
    """Two concurrent broker workers claiming simultaneously get disjoint rows across tenants."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        # Clear any leftover queued rows from earlier tests in this module
        with conn.cursor() as cur:
            cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
            cur.execute("UPDATE app.action_executions SET lease_expires_at = clock_timestamp() + interval '1 hour' WHERE status IN ('claimed', 'dispatched', 'uncertain')")
        t1, _, _, _, exec1, _ = _seed_execution(conn)
        t2, _, _, _, exec2, _ = _seed_execution(conn)

    SessionLocal = sessionmaker(bind=pg_engine, future=True)

    def _claim(worker_name: str) -> tuple[UUID | None, UUID | None]:
        with SessionLocal() as session:
            session.execute(text("SET LOCAL ROLE opspilot_broker"))
            repo = ExecutionRepository(session)
            row = repo.claim_next(lease_owner=worker_name, lease_seconds=30)
            if row is None:
                session.commit()
                return None, None
            eid, tid = row.id, row.tenant_id
            session.commit()
            return eid, tid

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_claim, "broker-a")
        f2 = pool.submit(_claim, "broker-b")
        r1 = f1.result()
        r2 = f2.result()

    claimed_ids = {r1[0], r2[0]}
    claimed_tenants = {r1[1], r2[1]}
    assert claimed_ids == {exec1, exec2}
    assert claimed_tenants == {t1, t2}


def test_execution_events_gap_free_and_append_only(pg_engine):
    """Full lifecycle produces gap-free seq 1..N and rejects UPDATE/DELETE on execution_events."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
            cur.execute("UPDATE app.action_executions SET lease_expires_at = clock_timestamp() + interval '1 hour' WHERE status IN ('claimed', 'dispatched', 'uncertain')")
        tenant_id, _, _, _, exec_id, _ = _seed_execution(conn)

    SessionLocal = sessionmaker(bind=pg_engine, future=True)
    with SessionLocal() as session:
        session.execute(text("SET LOCAL ROLE opspilot_broker"))
        repo = ExecutionRepository(session)
        ex = repo.claim_next(lease_owner="broker-seq", lease_seconds=30)
        assert ex is not None and ex.id == exec_id and ex.lease_token is not None
        tok = ex.lease_token

        ex = repo.mark_dispatched(exec_id, tok, target_state_before={"version": "v1.0.0"})
        ex = repo.mark_uncertain(exec_id, tok, last_error={"code": "timeout"})
        ex = repo.mark_dispatched(exec_id, tok, target_state_before={"version": "v1.0.0"})
        ex = repo.mark_acknowledged(exec_id, tok, target_response={"outcome": "applied"})
        ex = repo.mark_succeeded(exec_id, tok)
        assert ex.status == "succeeded"
        assert ex.attempts == 2
        session.commit()

    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq, type, tenant_id FROM app.execution_events WHERE execution_id = %s ORDER BY seq ASC",
                (exec_id,),
            )
            rows = cur.fetchall()
            seqs = [r[0] for r in rows]
            types = [r[1] for r in rows]
            assert seqs == [1, 2, 3, 4, 5, 6]
            assert types == [
                "execution_claimed",
                "execution_dispatched",
                "execution_uncertain",
                "execution_dispatched",
                "execution_acknowledged",
                "execution_succeeded",
            ]
            assert all(r[2] == tenant_id for r in rows)

            # Append-only trigger rejects UPDATE and DELETE
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("UPDATE app.execution_events SET type = 'tampered' WHERE execution_id = %s", (exec_id,))
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("DELETE FROM app.execution_events WHERE execution_id = %s", (exec_id,))


def test_run_allowed_transitions_include_broker_paths():
    """app.allowed_transitions contains all broker-driven run transitions."""
    with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT from_status, to_status FROM app.allowed_transitions")
            pairs = {(r[0], r[1]) for r in cur.fetchall()}

    for required in (
        ("approved", "executing"),
        ("approved", "escalated"),
        ("approved", "failed"),
        ("executing", "verifying"),
        ("executing", "escalated"),
        ("executing", "failed"),
    ):
        assert required in pairs
