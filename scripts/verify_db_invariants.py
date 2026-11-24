"""Script to execute each failing statement and capture exact Postgres error messages."""

import uuid

import psycopg

from apps.api.models.hashing import compute_plan_hash
from tools.common.evidence_store import compute_content_hash

DB_URL = "postgresql://opspilot@127.0.0.1:54333/opspilot"

conn = psycopg.connect(DB_URL, autocommit=True)
cur = conn.cursor()

# Set up test tenants
t1 = uuid.uuid4()
t2 = uuid.uuid4()
cur.execute(
    "INSERT INTO app.tenants (id, name, slug) VALUES (%s, 'Tenant A', %s)", (t1, f"ta-{t1.hex[:6]}")
)
cur.execute(
    "INSERT INTO app.tenants (id, name, slug) VALUES (%s, 'Tenant B', %s)", (t2, f"tb-{t2.hex[:6]}")
)

print("=" * 80)
print("LIVE POSTGRESQL ERROR MESSAGES ON INVARIANT VIOLATIONS")
print("=" * 80)

# Switch to opspilot_api role for tenant-bound operations
cur.execute("SET ROLE opspilot_api")
cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t1),))

# 1. RLS Violation: Cross-tenant INSERT
print("\n--- 1. RLS Violation: Cross-tenant INSERT into app.users ---")
try:
    u = uuid.uuid4()
    cur.execute(
        "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, 'viewer', 'hash')",
        (u, t2, f"cross-user-{u.hex[:6]}@example.com"),
    )
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# Setup incident and run for subsequent tests
inc_id = uuid.uuid4()
run_id = uuid.uuid4()
plan_id = uuid.uuid4()
obs_id = uuid.uuid4()
user_id = uuid.uuid4()

cur.execute(
    "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
    "VALUES (%s, %s, 'alert-inv-1', 'alert', 'web', 'HIGH', '{}'::jsonb, now(), 'open')",
    (inc_id, t1),
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
    "INSERT INTO app.users (id, tenant_id, email, role, password_hash) "
    "VALUES (%s, %s, %s, 'reviewer', 'hash')",
    (user_id, t1, f"reviewer-{user_id.hex[:6]}@example.com"),
)

# 2. Status Transition Violation: Illegal transition on app.runs
print(
    "\n--- 2. Status Transition Violation: Illegal transition 'queued' -> 'completed' on app.runs ---"
)
try:
    cur.execute("UPDATE app.runs SET status = 'completed' WHERE id = %s", (run_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# 3. Plan Hash Mismatch on INSERT into app.action_plans
print("\n--- 3. Plan Hash Mismatch: Corrupted plan_hash on INSERT into app.action_plans ---")
try:
    cur.execute(
        "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at) "
        "VALUES (%s, %s, %s, 'rollback_service', 'web', 'v1.0.0', ARRAY['ev-1'], 'revert', '0000000000000000000000000000000000000000000000000000000000000000', 'proposed', now() + interval '1 hour')",
        (plan_id, run_id, t1),
    )
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# Insert legitimate plan for immutability and approval tests
legit_hash = compute_plan_hash(
    action="rollback_service",
    service="web",
    expected_version="v1.0.0",
    rationale_evidence_ids=["ev-1"],
    rollback_route="revert",
)
cur.execute(
    "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at) "
    "VALUES (%s, %s, %s, 'rollback_service', 'web', 'v1.0.0', ARRAY['ev-1'], 'revert', %s, 'proposed', now() + interval '1 hour')",
    (plan_id, run_id, t1, legit_hash),
)

# 4. Plan Immutability Violation: Altering core plan fields on app.action_plans
print(
    "\n--- 4. Plan Immutability Violation: Attempting to update 'service' on app.action_plans ---"
)
try:
    cur.execute("UPDATE app.action_plans SET service = 'inventory' WHERE id = %s", (plan_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

print("\n--- 5. Plan Immutability Violation: Attempting to update 'action' on app.action_plans ---")
try:
    cur.execute("UPDATE app.action_plans SET action = 'restart_service' WHERE id = %s", (plan_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# 5. Append-Only Violation: UPDATE and DELETE on app.observations
print("\n--- 6. Append-Only Violation: Attempting to UPDATE app.observations ---")
obs_hash = compute_content_hash({})
cur.execute(
    "INSERT INTO app.observations (id, evidence_id, run_id, tenant_id, tool, source_id, captured_at, window_start, window_end, args, summary, payload, source_version, content_hash) "
    "VALUES (%s, 'ev-1', %s, %s, 'get_metrics', 'src-1', now(), now(), now(), '{}'::jsonb, 'Summary 1', '{}'::jsonb, 'v1', %s)",
    (obs_id, run_id, t1, obs_hash),
)
try:
    cur.execute("UPDATE app.observations SET summary = 'Modified Summary' WHERE id = %s", (obs_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

print("\n--- 7. Append-Only Violation: Attempting to DELETE from app.observations ---")
try:
    cur.execute("DELETE FROM app.observations WHERE id = %s", (obs_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# 6. Append-Only Violation: UPDATE and DELETE on app.run_events
print("\n--- 8. Append-Only Violation: Attempting to UPDATE app.run_events ---")
cur.execute(
    "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
    "VALUES (%s, %s, 1, 'agent_action', 'agent', '{}'::jsonb) RETURNING id",
    (run_id, t1),
)
evt_id = cur.fetchone()[0]
try:
    cur.execute("UPDATE app.run_events SET type = 'user_action' WHERE id = %s", (evt_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

print("\n--- 9. Append-Only Violation: Attempting to DELETE from app.run_events ---")
try:
    cur.execute("DELETE FROM app.run_events WHERE id = %s", (evt_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# 7. Delete Rejection Violation: DELETE on app.approvals
print("\n--- 10. Delete Rejection Violation: Attempting to DELETE from app.approvals ---")
appr_id = uuid.uuid4()
cur.execute(
    "INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, reason, decided_at) "
    "VALUES (%s, %s, %s, %s, 'approve', %s, 'Approved by lead', now())",
    (appr_id, plan_id, t1, user_id, legit_hash),
)
try:
    cur.execute("DELETE FROM app.approvals WHERE id = %s", (appr_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

# 8. Delete Rejection Violation: DELETE on app.action_executions
print("\n--- 11. Delete Rejection Violation: Attempting to DELETE from app.action_executions ---")
exec_id = uuid.uuid4()
cur.execute(
    "INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, status, attempts) "
    "VALUES (%s, %s, %s, 'rollback:web:1', 'queued', 0)",
    (exec_id, plan_id, t1),
)
try:
    cur.execute("DELETE FROM app.action_executions WHERE id = %s", (exec_id,))
except Exception as e:
    print(f"FAILED AS EXPECTED:\n{type(e).__name__}: {e}")

cur.close()
conn.close()
print("\n" + "=" * 80)
print("ALL INVARIANT ENFORCEMENT CHECKS EXECUTED SUCCESSFULLY")
print("=" * 80)
