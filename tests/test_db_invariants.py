import uuid

import psycopg
import pytest

from apps.api.models.hashing import compute_plan_hash
from tools.common.evidence_store import compute_content_hash

DB_URL = "postgresql://opspilot@127.0.0.1:54333/opspilot"


@pytest.fixture(scope="module")
def db_conn():
    conn = psycopg.connect(DB_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def tenant_ids(db_conn):
    t1 = uuid.uuid4()
    t2 = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (t1, "Tenant 1", f"t1-{t1.hex[:8]}"),
        )
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (t2, "Tenant 2", f"t2-{t2.hex[:8]}"),
        )
    return t1, t2


def test_rls_isolation_and_insert_rejection(db_conn, tenant_ids):
    t1, t2 = tenant_ids

    # As opspilot_api role (non-superuser, subject to RLS)
    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

        # Insert user for tenant 1
        u1 = uuid.uuid4()
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'viewer', 'hash')",
            (u1, t1, f"user1-{u1.hex[:6]}@example.com"),
        )

        # Select user - visible
        cur.execute("SELECT id FROM app.users WHERE id = %s", (u1,))
        assert cur.fetchone() is not None

        # Cross-tenant insert fails with RLS violation
        u2 = uuid.uuid4()
        with pytest.raises(psycopg.Error) as exc:
            cur.execute(
                "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'viewer', 'hash')",
                (u2, t2, f"user2-{u2.hex[:6]}@example.com"),
            )
        assert "row-level security policy" in str(exc.value).lower()
        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_rls_cross_tenant_select_returns_zero(db_conn, tenant_ids):
    t1, t2 = tenant_ids

    inc1 = uuid.uuid4()
    inc2 = uuid.uuid4()
    alert_payload = psycopg.types.json.Jsonb(
        {
            "alert_id": "a1",
            "service": "web",
            "metric_name": "cpu",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )

    # Insert incident for tenant 1 and tenant 2 as opspilot_api with corresponding tenant_id
    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")

        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a1', 'alert', 'web', 'CRITICAL', %s, now(), 'open')",
            (inc1, t1, alert_payload),
        )

        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t2),))
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a2', 'alert', 'orders', 'CRITICAL', %s, now(), 'open')",
            (inc2, t2, alert_payload),
        )

        # Now query as tenant 1: inc1 is visible, inc2 is invisible (returns 0 rows / not found)
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))
        cur.execute("SELECT id FROM app.incidents WHERE id = %s", (inc1,))
        assert cur.fetchone() is not None

        cur.execute("SELECT id FROM app.incidents WHERE id = %s", (inc2,))
        assert cur.fetchone() is None

        # Query all incidents - tenant 1 sees only inc1
        cur.execute("SELECT id FROM app.incidents")
        rows = [r[0] for r in cur.fetchall()]
        assert inc1 in rows
        assert inc2 not in rows

        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_plan_immutability_trigger(db_conn, tenant_ids):
    t1, _ = tenant_ids
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

        alert_payload = psycopg.types.json.Jsonb(
            {
                "alert_id": "a3",
                "service": "orders",
                "metric_name": "latency",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a3', 'alert', 'orders', 'HIGH', %s, now(), 'open')",
            (inc_id, t1, alert_payload),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version, remaining_calls, max_calls) "
            "VALUES (%s, %s, %s, 'queued', 1, 10, 10)",
            (run_id, t1, inc_id),
        )
        cur.execute(
            "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
            "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
            (run_id, t1),
        )

        plan_hash = compute_plan_hash(
            action="restart_service",
            service="orders",
            expected_version=None,
            rationale_evidence_ids=["ev-1", "ev-2"],
            rollback_route=None,
        )

        cur.execute(
            "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, rationale_evidence_ids, plan_hash, status, expires_at) "
            "VALUES (%s, %s, %s, 'restart_service', 'orders', ARRAY['ev-1', 'ev-2'], %s, 'proposed', now() + interval '1 hour')",
            (plan_id, run_id, t1, plan_hash),
        )

        # Attempt to modify immutable field: service
        with pytest.raises(psycopg.Error) as exc:
            cur.execute(
                "UPDATE app.action_plans SET service = 'inventory' WHERE id = %s", (plan_id,)
            )
        assert (
            "Immutable plan fields (action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, run_id, tenant_id) cannot be modified"
            in str(exc.value)
        )

        # Attempt to modify immutable field: action
        with pytest.raises(psycopg.Error) as exc:
            cur.execute(
                "UPDATE app.action_plans SET action = 'rollback_service' WHERE id = %s", (plan_id,)
            )
        assert "Immutable plan fields" in str(exc.value)

        # Legal update: status change
        cur.execute("UPDATE app.action_plans SET status = 'approved' WHERE id = %s", (plan_id,))
        cur.execute("SELECT status FROM app.action_plans WHERE id = %s", (plan_id,))
        assert cur.fetchone()[0] == "approved"

        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_run_status_transition_enforcement(db_conn, tenant_ids):
    t1, _ = tenant_ids
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()

    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

        alert_payload = psycopg.types.json.Jsonb(
            {
                "alert_id": "a4",
                "service": "checkout",
                "metric_name": "errors",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a4', 'alert', 'checkout', 'CRITICAL', %s, now(), 'open')",
            (inc_id, t1, alert_payload),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version, remaining_calls, max_calls) "
            "VALUES (%s, %s, %s, 'queued', 1, 10, 10)",
            (run_id, t1, inc_id),
        )
        cur.execute(
            "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
            "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
            (run_id, t1),
        )

        # Illegal transition: queued -> completed
        with pytest.raises(psycopg.Error) as exc:
            cur.execute("UPDATE app.runs SET status = 'completed' WHERE id = %s", (run_id,))
        assert 'Illegal run status transition from "queued" to "completed"' in str(exc.value)

        # Legal transition 1: queued -> investigating
        cur.execute("UPDATE app.runs SET status = 'investigating' WHERE id = %s", (run_id,))
        cur.execute("SELECT status, version FROM app.runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        assert row[0] == "investigating"
        assert row[1] == 2  # version incremented

        # Legal transition 2: investigating -> needs_review
        cur.execute("UPDATE app.runs SET status = 'needs_review' WHERE id = %s", (run_id,))
        cur.execute("SELECT status, version FROM app.runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        assert row[0] == "needs_review"
        assert row[1] == 3

        # Illegal backward transition: needs_review -> queued
        with pytest.raises(psycopg.Error) as exc:
            cur.execute("UPDATE app.runs SET status = 'queued' WHERE id = %s", (run_id,))
        assert 'Illegal run status transition from "needs_review" to "queued"' in str(exc.value)

        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_append_only_and_delete_rejection_triggers(db_conn, tenant_ids):
    t1, _ = tenant_ids
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    obs_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    appr_id = uuid.uuid4()
    user_id = uuid.uuid4()

    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

        alert_payload = psycopg.types.json.Jsonb(
            {
                "alert_id": "a5",
                "service": "web",
                "metric_name": "cpu",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a5', 'alert', 'web', 'LOW', %s, now(), 'open')",
            (inc_id, t1, alert_payload),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version, remaining_calls, max_calls) "
            "VALUES (%s, %s, %s, 'queued', 1, 10, 10)",
            (run_id, t1, inc_id),
        )
        cur.execute(
            "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
            "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
            (run_id, t1),
        )
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'reviewer', 'hash')",
            (user_id, t1, f"rev-{user_id.hex[:6]}@example.com"),
        )

        # 1. observations
        obs_dict = {"cpu": 95}
        obs_payload = psycopg.types.json.Jsonb(obs_dict)
        obs_hash = compute_content_hash(obs_dict)
        cur.execute(
            "INSERT INTO app.observations (id, evidence_id, run_id, tenant_id, tool, source_id, captured_at, window_start, window_end, args, summary, payload, source_version, content_hash) "
            "VALUES (%s, 'ev-1', %s, %s, 'get_metrics', 'metrics-1', now(), now(), now(), '{}'::jsonb, 'High CPU', %s, 'v1', %s)",
            (obs_id, run_id, t1, obs_payload, obs_hash),
        )

        with pytest.raises(psycopg.Error) as exc:
            cur.execute("UPDATE app.observations SET summary = 'Low CPU' WHERE id = %s", (obs_id,))
        assert 'Table "observations" is strictly append-only' in str(exc.value)

        with pytest.raises(psycopg.Error) as exc:
            cur.execute("DELETE FROM app.observations WHERE id = %s", (obs_id,))
        assert 'Table "observations" is strictly append-only' in str(exc.value)

        # 2. run_events
        evt_payload = psycopg.types.json.Jsonb({"tool": "get_metrics"})
        cur.execute(
            "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
            "VALUES (%s, %s, 2, 'tool_call', 'agent', %s) RETURNING id",
            (run_id, t1, evt_payload),
        )
        evt_id = cur.fetchone()[0]

        with pytest.raises(psycopg.Error) as exc:
            cur.execute("UPDATE app.run_events SET type = 'tool_result' WHERE id = %s", (evt_id,))
        assert 'Table "run_events" is strictly append-only' in str(exc.value)

        with pytest.raises(psycopg.Error) as exc:
            cur.execute("DELETE FROM app.run_events WHERE id = %s", (evt_id,))
        assert 'Table "run_events" is strictly append-only' in str(exc.value)

        # 3. approvals
        plan_hash = compute_plan_hash(
            action="restart_service",
            service="web",
            expected_version=None,
            rationale_evidence_ids=["ev-1"],
            rollback_route=None,
        )
        cur.execute(
            "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, rationale_evidence_ids, plan_hash, status, expires_at) "
            "VALUES (%s, %s, %s, 'restart_service', 'web', ARRAY['ev-1'], %s, 'proposed', now() + interval '1 hour')",
            (plan_id, run_id, t1, plan_hash),
        )
        cur.execute(
            "INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, reason, decided_at) "
            "VALUES (%s, %s, %s, %s, 'approve', %s, 'looks good', now())",
            (appr_id, plan_id, t1, user_id, plan_hash),
        )

        with pytest.raises(psycopg.Error) as exc:
            cur.execute("DELETE FROM app.approvals WHERE id = %s", (appr_id,))
        assert 'Deleting records from "approvals" is strictly forbidden' in str(exc.value)

        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


def test_plan_hash_verification_trigger(db_conn, tenant_ids):
    t1, _ = tenant_ids
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id1 = uuid.uuid4()
    plan_id2 = uuid.uuid4()

    with db_conn.cursor() as cur:
        cur.execute("SET ROLE opspilot_api")
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

        alert_payload = psycopg.types.json.Jsonb(
            {
                "alert_id": "a6",
                "service": "gateway",
                "metric_name": "errors",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, 'a6', 'alert', 'gateway', 'CRITICAL', %s, now(), 'open')",
            (inc_id, t1, alert_payload),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version, remaining_calls, max_calls) "
            "VALUES (%s, %s, %s, 'queued', 1, 10, 10)",
            (run_id, t1, inc_id),
        )
        cur.execute(
            "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
            "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
            (run_id, t1),
        )

        correct_hash = compute_plan_hash(
            action="rollback_service",
            service="gateway",
            expected_version="v1.2.3",
            rationale_evidence_ids=["ev-b", "ev-a"],
            rollback_route="git-revert",
        )

        # 1. Insert with mismatched / corrupt hash fails
        with pytest.raises(psycopg.Error) as exc:
            cur.execute(
                "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at) "
                "VALUES (%s, %s, %s, 'rollback_service', 'gateway', 'v1.2.3', ARRAY['ev-b', 'ev-a'], 'git-revert', 'badbadbad0000000000000000000000000000000000000000000000000000000', 'proposed', now() + interval '1 hour')",
                (plan_id1, run_id, t1),
            )
        assert "Plan hash mismatch on insert" in str(exc.value)

        # 2. Insert with correct canonical hash succeeds
        cur.execute(
            "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at) "
            "VALUES (%s, %s, %s, 'rollback_service', 'gateway', 'v1.2.3', ARRAY['ev-b', 'ev-a'], 'git-revert', %s, 'proposed', now() + interval '1 hour')",
            (plan_id2, run_id, t1, correct_hash),
        )

        cur.execute("SELECT plan_hash FROM app.action_plans WHERE id = %s", (plan_id2,))
        assert cur.fetchone()[0] == correct_hash

        cur.execute("RESET ROLE")
        cur.execute("SELECT set_config('app.tenant_id', '', false)")
