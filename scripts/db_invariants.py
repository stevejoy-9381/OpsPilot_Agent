"""Database Invariant Enforcement & Integrity Verifier.

Queries that must return zero rows:
1. Runs without events: app.runs without any matching app.run_events.
2. Approved plans without matching approval row in terminal states:
   app.action_plans with status in ('approved', 'executed') on runs in terminal states
   ('completed', 'escalated', 'failed') without a corresponding 'approve' decision in app.approvals.
3. Executions without plans: app.action_executions without a parent app.action_plans record.
4. Observations whose content_hash does not match: app.observations whose stored content_hash
   does not match deterministic compute_content_hash(payload or payload_ref).

Exits with code 0 on zero violations, code 1 if violations exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from psycopg.rows import dict_row

from tools.common.evidence_store import compute_content_hash

DEFAULT_DB_URL = "postgresql://opspilot:opspilot_dev_pass@127.0.0.1:54333/opspilot"


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", DEFAULT_DB_URL)
    return url.replace("+psycopg", "")


def check_invariants() -> int:
    db_url = get_db_url()
    violations_found = 0

    print("=" * 80)
    print("OPSPILOT DATABASE INVARIANTS VERIFICATION")
    print("=" * 80)
    print(f"Target Database: {db_url}")
    print()

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # ---------------------------------------------------------------
            # Check 1: Runs without events
            # ---------------------------------------------------------------
            print("[CHECK 1] Runs without events (every run must record >= 1 event)")
            cur.execute(
                """
                SELECT r.id, r.tenant_id, r.status, r.created_at
                FROM app.runs r
                LEFT JOIN app.run_events e ON r.id = e.run_id
                WHERE e.id IS NULL;
                """
            )
            orphan_runs = cur.fetchall()
            if orphan_runs:
                violations_found += len(orphan_runs)
                print(f"  FAILED: Found {len(orphan_runs)} runs without events:")
                for r in orphan_runs:
                    print(f"    - Run ID: {r['id']}, Tenant: {r['tenant_id']}, Status: {r['status']}")
            else:
                print("  PASSED: 0 runs without events.")
            print()

            # ---------------------------------------------------------------
            # Check 2: Approved plans without matching approval in terminal states
            # ---------------------------------------------------------------
            print("[CHECK 2] Approved plans without matching approval row in terminal states")
            cur.execute(
                """
                SELECT p.id as plan_id, p.run_id, p.tenant_id, p.status as plan_status, r.status as run_status
                FROM app.action_plans p
                JOIN app.runs r ON p.run_id = r.id
                WHERE p.status IN ('approved', 'executed')
                  AND r.status IN ('completed', 'escalated', 'failed')
                  AND NOT EXISTS (
                      SELECT 1 FROM app.approvals a
                      WHERE a.plan_id = p.id AND a.decision = 'approve'
                  );
                """
            )
            unapproved_plans = cur.fetchall()
            if unapproved_plans:
                violations_found += len(unapproved_plans)
                print(f"  FAILED: Found {len(unapproved_plans)} approved plans lacking approval records in terminal runs:")
                for p in unapproved_plans:
                    print(f"    - Plan ID: {p['plan_id']}, Run ID: {p['run_id']}, Plan Status: {p['plan_status']}, Run Status: {p['run_status']}")
            else:
                print("  PASSED: 0 approved plans lacking approval records in terminal states.")
            print()

            # ---------------------------------------------------------------
            # Check 3: Executions without plans
            # ---------------------------------------------------------------
            print("[CHECK 3] Executions without parent action plans")
            cur.execute(
                """
                SELECT e.id, e.action_key, e.plan_id, e.tenant_id
                FROM app.action_executions e
                LEFT JOIN app.action_plans p ON e.plan_id = p.id
                WHERE p.id IS NULL;
                """
            )
            orphan_executions = cur.fetchall()
            if orphan_executions:
                violations_found += len(orphan_executions)
                print(f"  FAILED: Found {len(orphan_executions)} action executions without parent plans:")
                for e in orphan_executions:
                    print(f"    - Execution ID: {e['id']}, Action Key: {e['action_key']}, Plan ID: {e['plan_id']}")
            else:
                print("  PASSED: 0 orphan action executions.")
            print()

            # ---------------------------------------------------------------
            # Check 4: Observations whose content_hash does not match
            # ---------------------------------------------------------------
            print("[CHECK 4] Observations with corrupted or mismatched content_hash")
            cur.execute(
                """
                SELECT id, evidence_id, run_id, tenant_id, payload, payload_ref, content_hash
                FROM app.observations;
                """
            )
            observations = cur.fetchall()
            mismatched_observations = []

            for obs in observations:
                payload = obs["payload"]
                payload_ref = obs["payload_ref"]
                if payload_ref and Path(payload_ref).exists():
                    try:
                        import json
                        payload = json.loads(Path(payload_ref).read_text(encoding="utf-8"))
                    except Exception:
                        pass
                recomputed = compute_content_hash(payload if payload is not None else payload_ref)
                if recomputed != obs["content_hash"]:
                    mismatched_observations.append({
                        "id": obs["id"],
                        "evidence_id": obs["evidence_id"],
                        "run_id": obs["run_id"],
                        "stored_hash": obs["content_hash"],
                        "recomputed_hash": recomputed,
                    })

            if mismatched_observations:
                violations_found += len(mismatched_observations)
                print(f"  FAILED: Found {len(mismatched_observations)} observations with content_hash mismatches:")
                for m in mismatched_observations:
                    print(f"    - Observation {m['id']} (evidence_id={m['evidence_id']}, run_id={m['run_id']}): stored={m['stored_hash']} recomputed={m['recomputed_hash']}")
            else:
                print(f"  PASSED: {len(observations)} observations verified with 0 content_hash mismatches.")
            print()

            # ---------------------------------------------------------------
            # Check 5: Approved plans without exactly one approval row
            # ---------------------------------------------------------------
            print("[CHECK 5] Approved plans without exactly one 'approve' row")
            cur.execute(
                """
                SELECT p.id AS plan_id, p.run_id, COUNT(a.id) AS approval_count
                FROM app.action_plans p
                LEFT JOIN app.approvals a ON a.plan_id = p.id AND a.decision = 'approve'
                WHERE p.status IN ('approved', 'executed')
                GROUP BY p.id, p.run_id
                HAVING COUNT(a.id) <> 1;
                """
            )
            bad_approval_counts = cur.fetchall()
            if bad_approval_counts:
                violations_found += len(bad_approval_counts)
                print(f"  FAILED: Found {len(bad_approval_counts)} approved plans with != 1 approval row")
            else:
                print("  PASSED: Every approved/executed plan has exactly 1 approve row.")
            print()

            # ---------------------------------------------------------------
            # Check 6: Executions without an approved/executed plan
            # ---------------------------------------------------------------
            print("[CHECK 6] Action executions without an approved/executed plan or approval row")
            cur.execute(
                """
                SELECT e.id AS exec_id, e.plan_id, p.status AS plan_status
                FROM app.action_executions e
                JOIN app.action_plans p ON e.plan_id = p.id
                WHERE p.status NOT IN ('approved', 'executed')
                   OR NOT EXISTS (
                       SELECT 1 FROM app.approvals a
                       WHERE a.plan_id = p.id AND a.decision = 'approve'
                   );
                """
            )
            unapproved_execs = cur.fetchall()
            if unapproved_execs:
                violations_found += len(unapproved_execs)
                print(f"  FAILED: Found {len(unapproved_execs)} executions on non-approved plans")
            else:
                print("  PASSED: 0 executions on non-approved plans.")
            print()

            # ---------------------------------------------------------------
            # Check 7: Plans approved after expires_at
            # ---------------------------------------------------------------
            print("[CHECK 7] Plans approved after expires_at")
            cur.execute(
                """
                SELECT p.id AS plan_id, a.decided_at, p.expires_at
                FROM app.approvals a
                JOIN app.action_plans p ON a.plan_id = p.id
                WHERE a.decision = 'approve' AND a.decided_at > p.expires_at;
                """
            )
            late_approvals = cur.fetchall()
            if late_approvals:
                violations_found += len(late_approvals)
                print(f"  FAILED: Found {len(late_approvals)} plans approved after expires_at")
            else:
                print("  PASSED: 0 plans approved after expires_at.")
            print()

            # ---------------------------------------------------------------
            # Check 8: Runs in needs_review whose plan is not proposed
            # ---------------------------------------------------------------
            print("[CHECK 8] Runs in needs_review without a proposed plan")
            cur.execute(
                """
                SELECT r.id AS run_id
                FROM app.runs r
                WHERE r.status = 'needs_review'
                  AND NOT EXISTS (
                      SELECT 1 FROM app.action_plans p
                      WHERE p.run_id = r.id AND p.status = 'proposed'
                  );
                """
            )
            bad_review_runs = cur.fetchall()
            if bad_review_runs:
                violations_found += len(bad_review_runs)
                print(f"  FAILED: Found {len(bad_review_runs)} needs_review runs without a proposed plan")
            else:
                print("  PASSED: Every needs_review run has a proposed plan.")
            print()

            # ---------------------------------------------------------------
            # Check 9: Runs with two or more active (proposed) plans
            # ---------------------------------------------------------------
            print("[CHECK 9] Runs with two or more active (proposed) plans")
            cur.execute(
                """
                SELECT run_id, COUNT(*) AS active_count
                FROM app.action_plans
                WHERE status = 'proposed'
                GROUP BY run_id
                HAVING COUNT(*) > 1;
                """
            )
            multi_active = cur.fetchall()
            if multi_active:
                violations_found += len(multi_active)
                print(f"  FAILED: Found {len(multi_active)} runs with >= 2 active proposed plans")
            else:
                print("  PASSED: 0 runs with multiple active proposed plans.")
            print()

            # ---------------------------------------------------------------
            # Check 10: Executions in dispatched state older than the deadline
            # ---------------------------------------------------------------
            deadline_seconds = int(os.environ.get("BROKER_RECONCILE_DEADLINE_SECONDS", "60"))
            print(f"[CHECK 10] Executions in dispatched state older than {deadline_seconds}s")
            cur.execute(
                """
                SELECT e.id AS exec_id, e.plan_id, e.dispatched_at
                FROM app.action_executions e
                WHERE e.status = 'dispatched'
                  AND (
                      e.dispatched_at IS NULL
                      OR e.dispatched_at < clock_timestamp() - make_interval(secs => %s)
                  );
                """,
                (deadline_seconds,),
            )
            stale_dispatched = cur.fetchall()
            if stale_dispatched:
                violations_found += len(stale_dispatched)
                print(f"  FAILED: Found {len(stale_dispatched)} dispatched executions older than deadline")
            else:
                print("  PASSED: 0 stale dispatched executions.")
            print()

            # ---------------------------------------------------------------
            # Check 11: Two or more non-terminal executions for one target
            # ---------------------------------------------------------------
            print("[CHECK 11] Targets with two or more non-terminal executions")
            cur.execute(
                """
                SELECT COALESCE(e.target_id, p.target_id) AS target_id, COUNT(*) AS active_count
                FROM app.action_executions e
                JOIN app.action_plans p ON e.plan_id = p.id
                WHERE e.status IN ('queued', 'claimed', 'dispatched', 'uncertain')
                GROUP BY COALESCE(e.target_id, p.target_id)
                HAVING COUNT(*) > 1;
                """
            )
            multi_target_execs = cur.fetchall()
            if multi_target_execs:
                violations_found += len(multi_target_execs)
                print(f"  FAILED: Found {len(multi_target_execs)} targets with >= 2 non-terminal executions")
            else:
                print("  PASSED: 0 targets with multiple non-terminal executions.")
            print()

            # ---------------------------------------------------------------
            # Check 12 (live mode only): Acknowledged executions without lab.applied_actions row
            # ---------------------------------------------------------------
            live_mode = "--live" in sys.argv or bool(os.environ.get("LAB_DATABASE_URL"))
            if live_mode:
                lab_db_url = os.environ.get(
                    "LAB_DATABASE_URL",
                    "postgresql://lab_admin:lab-postgres-local-pw@127.0.0.1:5432/lab",
                ).replace("+psycopg", "")
                print("[CHECK 12] Live mode: Acknowledged executions without lab.applied_actions row")
                cur.execute(
                    """
                    SELECT id AS exec_id, action_key, status
                    FROM app.action_executions
                    WHERE status IN ('acknowledged', 'reconciled', 'succeeded');
                    """
                )
                acked_execs = cur.fetchall()
                with psycopg.connect(lab_db_url, row_factory=dict_row) as lab_conn:
                    with lab_conn.cursor() as lab_cur:
                        lab_cur.execute("SELECT action_key FROM lab.applied_actions;")
                        applied_keys = {row["action_key"] for row in lab_cur.fetchall()}
                missing_applied = [e for e in acked_execs if e["action_key"] not in applied_keys]
                if missing_applied:
                    violations_found += len(missing_applied)
                    print(
                        f"  FAILED: Found {len(missing_applied)} acknowledged executions missing from lab.applied_actions"
                    )
                else:
                    print(
                        f"  PASSED: All {len(acked_execs)} acknowledged executions present in lab.applied_actions."
                    )
                print()

    print("=" * 80)
    if violations_found == 0:
        print("INVARIANT VERIFICATION RESULT: ZERO VIOLATIONS DETECTED (PASSED)")
        print("=" * 80)
        return 0
    else:
        print(f"INVARIANT VERIFICATION RESULT: {violations_found} VIOLATIONS DETECTED (FAILED)")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    sys.exit(check_invariants())
