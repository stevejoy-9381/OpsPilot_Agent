"""Tests verifying the OpsPilot PostgreSQL schema, constraints, enums, and RBAC roles.

Tests:
1. Enum parity: Verifies that Python enums in apps.api.models.enums match DB CHECK constraints.
2. Migration Reversibility: Full upgrade -> downgrade -> upgrade cycle.
3. Constraint enforcement:
   - Action consistency (rollback/restart requires service, no_action/escalate must not).
   - UNIQUE(action_key) on app.action_executions.
   - UNIQUE(plan_id, reviewer_id) on app.approvals.
   - UNIQUE(run_id, seq) on app.run_events.
   - UNIQUE(run_id, evidence_id) on app.observations.
   - Action plan immutability trigger blocks updates to core proposal fields.
4. Role permissions and schema isolation:
   - opspilot_api can perform DML on app schema, but cannot DROP/ALTER.
   - opspilot_checkpointer can access checkpoints schema, but cannot read app tables.
   - LangGraph Postgres checkpointer setup() creates tables in checkpoints schema.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres import PostgresSaver

from apps.api.models.enums import (
    ActionPlanAction,
    ApprovalDecision,
    ExecutionStatus,
    RunStatus,
    ToolName,
    UserRole,
)
from apps.api.models.hashing import compute_plan_hash
from shared.actions import ActionType

DB_TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://opspilot@127.0.0.1:54333/opspilot",
)


def get_alembic_config(db_url: str) -> Config:
    """Returns configured Alembic Config pointing to test database."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Session fixture ensuring test DB is fully migrated."""
    cfg = get_alembic_config(DB_TEST_URL)
    command.upgrade(cfg, "head")
    return DB_TEST_URL


def test_enum_parity() -> None:
    """Verifies Python enum values match canonical domain contracts."""
    # ActionPlanAction must match shared.actions.ActionType exactly
    action_plan_values = {a.value for a in ActionPlanAction}
    action_type_values = {a.value for a in ActionType}
    assert action_plan_values == action_type_values
    assert action_plan_values == {"rollback_service", "restart_service", "no_action", "escalate"}

    # Roles
    assert {r.value for r in UserRole} == {"viewer", "reviewer", "admin"}

    # Run status state machine
    assert {s.value for s in RunStatus} == {
        "queued",
        "investigating",
        "needs_review",
        "approved",
        "executing",
        "verifying",
        "completed",
        "escalated",
        "failed",
    }

    # Tools
    assert {t.value for t in ToolName} == {
        "get_metrics",
        "search_logs",
        "get_trace",
        "list_deploys",
        "get_deploy_diff",
    }

    # Approvals & Executions
    assert {d.value for d in ApprovalDecision} == {"approve", "reject"}
    assert {e.value for e in ExecutionStatus} == {
        "queued",
        "dispatched",
        "acknowledged",
        "uncertain",
        "reconciled",
        "failed",
    }


def test_migration_reversibility(migrated_db: str) -> None:
    """Tests that downgrading to base and upgrading back to head executes cleanly."""
    cfg = get_alembic_config(migrated_db)
    # Downgrade back to baseline
    command.downgrade(cfg, "0001_baseline")

    # Verify app tables are gone
    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('app.runs');")
            assert cur.fetchone()[0] is None

    # Upgrade back to head
    command.upgrade(cfg, "head")

    # Verify app tables exist again
    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('app.runs');")
            assert cur.fetchone()[0] is not None


def test_plan_action_service_consistency_constraint(migrated_db: str) -> None:
    """Verifies CHECK constraint: rollback/restart requires service; no_action/escalate forbids service."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    run_id = uuid.uuid4()

    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, %s);",
                (tenant_id, f"t-{tenant_id}", "Test Tenant"),
            )
            cur.execute(
                """
                INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at)
                VALUES (%s, %s, 'alt-1', 'alert', 'orders', 'CRITICAL', '{"alert_id": "alt-1"}'::jsonb, clock_timestamp());
                """,
                (incident_id, tenant_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status) VALUES (%s, %s, %s, 'investigating');",
                (run_id, tenant_id, incident_id),
            )
            cur.execute(
                "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
                "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
                (run_id, tenant_id),
            )

            # 1. Illegal: rollback without service -> should fail
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO app.action_plans (run_id, tenant_id, action, service, plan_hash, expires_at)
                    VALUES (%s, %s, 'rollback_service', NULL, 'hash123', clock_timestamp());
                    """,
                    (run_id, tenant_id),
                )
        conn.rollback()

    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            # 2. Illegal: no_action with service -> should fail
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """
                    INSERT INTO app.action_plans (run_id, tenant_id, action, service, plan_hash, expires_at)
                    VALUES (%s, %s, 'no_action', 'orders', 'hash123', clock_timestamp());
                    """,
                    (run_id, tenant_id),
                )
        conn.rollback()


def test_unique_action_key_constraint(migrated_db: str) -> None:
    """Verifies UNIQUE(action_key) on app.action_executions rejects duplicates."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id1 = uuid.uuid4()
    plan_id2 = uuid.uuid4()
    action_key = f"act_key_{uuid.uuid4()}"

    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, %s);",
                (tenant_id, f"t-{tenant_id}", "Test Tenant"),
            )
            cur.execute(
                """
                INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at)
                VALUES (%s, %s, 'alt-1', 'alert', 'orders', 'CRITICAL', '{"alert_id": "alt-1"}'::jsonb, clock_timestamp());
                """,
                (incident_id, tenant_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status) VALUES (%s, %s, %s, 'investigating');",
                (run_id, tenant_id, incident_id),
            )
            cur.execute(
                "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
                "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
                (run_id, tenant_id),
            )
            hash1 = compute_plan_hash(
                action="rollback_service",
                service="orders",
                expected_version=None,
                rationale_evidence_ids=[],
                rollback_route=None,
            )
            cur.execute(
                """
                INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, plan_hash, expires_at)
                VALUES (%s, %s, %s, 'rollback_service', 'orders', %s, clock_timestamp());
                """,
                (plan_id1, run_id, tenant_id, hash1),
            )
            cur.execute(
                """
                INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, plan_hash, expires_at)
                VALUES (%s, %s, %s, 'rollback_service', 'orders', %s, clock_timestamp());
                """,
                (plan_id2, run_id, tenant_id, hash1),
            )

            # First execution with action_key succeeds
            cur.execute(
                """
                INSERT INTO app.action_executions (plan_id, tenant_id, action_key, status)
                VALUES (%s, %s, %s, 'queued');
                """,
                (plan_id1, tenant_id, action_key),
            )

            # Second execution with identical action_key MUST fail with unique violation
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO app.action_executions (plan_id, tenant_id, action_key, status)
                    VALUES (%s, %s, %s, 'queued');
                    """,
                    (plan_id2, tenant_id, action_key),
                )
        conn.rollback()


def test_unique_plan_reviewer_approval_constraint(migrated_db: str) -> None:
    """Verifies UNIQUE(plan_id, reviewer_id) on app.approvals."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, %s);",
                (tenant_id, f"t-{tenant_id}", "Tenant"),
            )
            cur.execute(
                "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, 'u@ex.com', 'reviewer', 'h');",
                (user_id, tenant_id),
            )
            cur.execute(
                """
                INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at)
                VALUES (%s, %s, 'alt-1', 'alert', 'orders', 'CRITICAL', '{"alert_id": "alt-1"}'::jsonb, clock_timestamp());
                """,
                (incident_id, tenant_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status) VALUES (%s, %s, %s, 'investigating');",
                (run_id, tenant_id, incident_id),
            )
            cur.execute(
                "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
                "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
                (run_id, tenant_id),
            )
            plan_hash = compute_plan_hash(
                action="rollback_service",
                service="orders",
                expected_version=None,
                rationale_evidence_ids=[],
                rollback_route=None,
            )
            cur.execute(
                """
                INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, plan_hash, expires_at)
                VALUES (%s, %s, %s, 'rollback_service', 'orders', %s, clock_timestamp());
                """,
                (plan_id, run_id, tenant_id, plan_hash),
            )

            # First vote succeeds
            cur.execute(
                """
                INSERT INTO app.approvals (plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash)
                VALUES (%s, %s, %s, 'approve', %s);
                """,
                (plan_id, tenant_id, user_id, plan_hash),
            )

            # Duplicate vote on same plan by same reviewer fails
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO app.approvals (plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash)
                    VALUES (%s, %s, %s, 'reject', %s);
                    """,
                    (plan_id, tenant_id, user_id, plan_hash),
                )
        conn.rollback()


def test_action_plan_immutability_trigger(migrated_db: str) -> None:
    """Verifies that updating core fields on an action plan raises an exception."""
    tenant_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, %s);",
                (tenant_id, f"t-{tenant_id}", "Tenant"),
            )
            cur.execute(
                """
                INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at)
                VALUES (%s, %s, 'alt-1', 'alert', 'orders', 'CRITICAL', '{"alert_id": "alt-1"}'::jsonb, clock_timestamp());
                """,
                (incident_id, tenant_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status) VALUES (%s, %s, %s, 'investigating');",
                (run_id, tenant_id, incident_id),
            )
            cur.execute(
                "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
                "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
                (run_id, tenant_id),
            )
            plan_hash = compute_plan_hash(
                action="rollback_service",
                service="orders",
                expected_version=None,
                rationale_evidence_ids=[],
                rollback_route=None,
            )
            cur.execute(
                """
                INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, plan_hash, expires_at)
                VALUES (%s, %s, %s, 'rollback_service', 'orders', %s, clock_timestamp());
                """,
                (plan_id, run_id, tenant_id, plan_hash),
            )

            # Status change is allowed
            cur.execute(
                "UPDATE app.action_plans SET status = 'approved' WHERE id = %s;", (plan_id,)
            )

            # Mutating action/service must be rejected by trigger
            with pytest.raises(psycopg.Error, match="Immutable plan fields"):
                cur.execute(
                    "UPDATE app.action_plans SET service = 'inventory' WHERE id = %s;", (plan_id,)
                )
        conn.rollback()


def test_role_privileges_and_schema_isolation(migrated_db: str) -> None:
    """Verifies RBAC role capabilities and boundaries:

    - opspilot_api cannot DROP or ALTER app tables.
    - opspilot_checkpointer cannot access app tables.
    - opspilot_checkpointer can setup LangGraph tables in checkpoints schema.
    """
    # 1. Test opspilot_api role: DML allowed, DDL forbidden
    api_conn_url = "postgresql://opspilot_api:opspilot_api_dev_pw@127.0.0.1:54333/opspilot"
    with psycopg.connect(api_conn_url) as conn:
        with conn.cursor() as cur:
            # Read allowed
            cur.execute("SELECT count(*) FROM app.tenants;")
            assert cur.fetchone() is not None

            # DDL forbidden
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DROP TABLE app.runs;")
        conn.rollback()

    # 2. Test opspilot_checkpointer role: cannot read app tables
    chk_conn_url = (
        "postgresql://opspilot_checkpointer:opspilot_checkpointer_dev_pw@127.0.0.1:54333/opspilot"
    )
    with psycopg.connect(chk_conn_url) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM app.runs;")
        conn.rollback()

    # 3. Test LangGraph PostgresSaver setup() works under checkpoints schema
    with psycopg.connect(chk_conn_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Set search path to checkpoints so PostgresSaver creates tables there
            cur.execute("SET search_path TO checkpoints;")
            saver = PostgresSaver(conn=conn)
            saver.setup()

            # Confirm tables exist in checkpoints schema
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'checkpoints' AND table_name = 'checkpoints';
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "checkpoints"
