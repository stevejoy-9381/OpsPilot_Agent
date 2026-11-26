"""Phase 9.9: Live Broker Crash Matrix (SIGKILL) + 20-Repeat Durability Suite.

Proves:
- Crash recovery across all 6 points (a..f) with real SIGKILL of the broker process.
- Reconcile before retry after dropped response (drop_response_after_apply).
- Zero duplicate effects, zero unapproved writes, zero wrong-target actions, and zero
  disagreements across 20 seeded mixed-fault iterations.
- Time-to-recover (median & max) per crash point and artifact timelines under
  evals/results/broker/<timestamp>/.
"""

from __future__ import annotations

import json
import os
import random
import signal
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.auth.dependencies import SESSION_COOKIE_NAME, get_db_session
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.domain.context import TenantContext
from apps.api.main import app
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    DiagnosisModel,
    IncidentModel,
    RunModel,
    SessionModel,
    TenantModel,
    UserModel,
)
from apps.api.repositories.run_repository import RunRepository
from apps.api.routers.plans import get_target_reader
from apps.api.services.run_service import RunService
from broker.target_client import LabAdminClient
from evals.graders.safety import grade_safety_from_db, grade_safety_records
from shared.plans import plan_hash
from shared.targets import DEFAULT_LAB_TARGETS_PATH, load_target_registry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_BASE = REPO_ROOT / "evals" / "results" / "broker"

PG_URL = os.environ.get(
    "TEST_DB_URL",
    "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:54333/opspilot",
)
PSYCOPG_URL = PG_URL.replace("postgresql+psycopg://", "postgresql://")
LAB_DB_URL = os.environ.get(
    "LAB_DATABASE_URL",
    "postgresql://lab_admin:lab-postgres-local-pw@lab-postgres:5432/lab",
).replace("+psycopg", "")


def _pg_available() -> bool:
    try:
        eng = create_engine(PG_URL, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _drain_active_executions(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
        cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE status = 'dispatched'")
        cur.execute(
            "UPDATE app.action_executions SET status = 'failed' WHERE status IN ('claimed', 'uncertain')"
        )
    conn.commit()


def _set_orders_version(version: str, admin_token: str) -> None:
    resp = httpx.post(
        "http://orders:8000/admin/deploy",
        headers={"X-Lab-Admin-Token": admin_token},
        json={"version": version, "actor": "phase9_9_setup"},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text


def _set_drop_hook(enabled: bool, admin_token: str) -> None:
    resp = httpx.post(
        "http://orders:8000/admin/failure-hooks",
        headers={"X-Lab-Admin-Token": admin_token},
        json={"drop_response_after_apply": enabled},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text


def _create_approved_execution(
    SessionLocal: sessionmaker[Any],
    client: TestClient,
    *,
    case_label: str,
) -> tuple[UUID, UUID, UUID, UUID, str, str]:
    with SessionLocal() as session:
        tenant_id = uuid4()
        user_id = uuid4()
        incident_id = uuid4()
        run_id = uuid4()
        plan_id = uuid4()
        session.add(
            TenantModel(id=tenant_id, name=f"P99 {case_label}", slug=f"p99-{tenant_id.hex[:8]}")
        )
        session.add(
            UserModel(
                id=user_id,
                tenant_id=tenant_id,
                email=f"rev-{user_id.hex[:6]}@example.com",
                role="reviewer",
                password_hash="hash",
            )
        )
        raw_token = generate_session_token()
        session.add(
            SessionModel(
                tenant_id=tenant_id,
                user_id=user_id,
                token_hash=hash_session_token(raw_token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        session.flush()
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        session.add(
            IncidentModel(
                id=incident_id,
                tenant_id=tenant_id,
                alert_id=f"alt-{uuid4().hex[:8]}",
                source="seeded_case",
                service="orders",
                severity="HIGH",
                alert_payload={"case_id": case_label},
                opened_at=datetime.now(timezone.utc),
                status_summary="open",
            )
        )
        session.flush()
        ctx = TenantContext(tenant_id=tenant_id, role="worker")
        run_repo = RunRepository(session)
        run_svc = RunService(session)
        run = run_repo.create(
            ctx=ctx,
            run=RunModel(
                id=run_id,
                tenant_id=tenant_id,
                incident_id=incident_id,
                case_id=case_label,
                status="queued",
            ),
        )
        run_svc.append_event(ctx, run_id, "run_created", "system", {"case_id": case_label})
        run, _ = run_svc.transition(
            ctx=ctx, run_id=run_id, to_status="investigating", actor="worker", expected_version=run.version
        )
        ev_id = f"ev_{uuid4().hex[:8]}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
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
        session.add(
            DiagnosisModel(
                run_id=run_id,
                tenant_id=tenant_id,
                root_cause_service="orders",
                root_cause_category="bad_deploy",
                mechanism="connection_pool_exhaustion",
                evidence_ids=[ev_id],
                confidence=0.95,
                outcome_kind="diagnosed",
                outcome_reason="concluded_by_model",
            )
        )
        session.add(
            ActionPlanModel(
                id=plan_id,
                run_id=run_id,
                tenant_id=tenant_id,
                action="rollback_service",
                service="orders",
                target_id="lab-orders",
                expected_version="v1.3.0",
                rollback_to_version="v1.0.0",
                rationale_evidence_ids=[ev_id],
                notes=f"Phase 9.9 {case_label}",
                status="proposed",
                expires_at=expires_at,
                plan_hash=p_hash,
            )
        )
        run, _ = run_svc.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="needs_review",
            actor="worker",
            expected_version=run.version,
            payload={"plan_id": str(plan_id)},
        )
        session.commit()

    resp = client.post(
        f"/plans/{plan_id}/decisions",
        headers={
            "Idempotency-Key": f"idem-{case_label}-{uuid4().hex[:8]}",
            "If-Match": "3",
            "X-CSRF-Token": "test-csrf",
            "X-Requested-With": "XMLHttpRequest",
        },
        cookies={SESSION_COOKIE_NAME: raw_token, "opspilot_csrf": "test-csrf"},
        json={
            "decision": "approve",
            "submitted_plan_hash": p_hash,
            "reason": f"Phase 9.9 {case_label} approved",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return tenant_id, run_id, plan_id, UUID(data["action_execution_id"]), data["action_key"], raw_token


def _spawn_broker(
    *,
    broker_id: str,
    crash_at: str | None = None,
    healthz_port: int = 8091,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = PG_URL
    env["LAB_ADMIN_TOKEN"] = os.environ.get("LAB_ADMIN_TOKEN", "super-secret-admin-token")
    env["BROKER_ID"] = broker_id
    env["BROKER_LEASE_SECONDS"] = "2"
    env["BROKER_HEARTBEAT_SECONDS"] = "1.0"
    env["BROKER_POLL_INTERVAL_SECONDS"] = "0.1"
    env["BROKER_HEALTHZ_PORT"] = str(healthz_port)
    env["BROKER_RECONCILE_DEADLINE_SECONDS"] = "15.0"
    if crash_at is not None:
        env["BROKER_CRASH_AT"] = crash_at
    else:
        env.pop("BROKER_CRASH_AT", None)

    return subprocess.Popen(
        [sys.executable, "-m", "broker.main"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _sigkill_on_marker(proc: subprocess.Popen[str], marker: str, timeout_s: float = 120.0) -> float:
    """Waits up to timeout_s for marker on stdout, then sends SIGKILL and returns kill timestamp."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_s
    lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if marker in line:
                return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=max(0.1, deadline - time.monotonic()))
    if t.is_alive():
        proc.kill()
        proc.wait(timeout=5)
        raise TimeoutError(f"Timed out waiting for marker {marker}; output={''.join(lines[-20:])}")

    kill_ts = time.monotonic()
    if hasattr(signal, "SIGKILL"):
        os.kill(proc.pid, signal.SIGKILL)
    else:
        proc.kill()
    proc.wait(timeout=10)
    return kill_ts


def _wait_for_execution_terminal(
    SessionLocal: sessionmaker[Any],
    exec_id: UUID,
    run_id: UUID,
    *,
    timeout_s: float = 120.0,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec_id)
            r = s.get(RunModel, run_id)
            if (
                ex is not None
                and r is not None
                and ex.status in ("acknowledged", "reconciled", "failed", "refused")
                and r.status in ("verifying", "escalated", "completed", "failed")
            ):
                return ex.status, r.status
        time.sleep(0.2)
    raise TimeoutError(f"Execution {exec_id} / Run {run_id} did not reach terminal state within {timeout_s}s")


def _count_lab_effects(action_key: str) -> tuple[int, int]:
    with psycopg.connect(LAB_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM lab.applied_actions WHERE action_key = %s", (action_key,))
            applied_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM lab.deploys WHERE service = 'orders' AND new_version = 'v1.0.0'"
            )
            deploys_count = int(cur.fetchone()[0])
    return applied_count, deploys_count


def test_grade_safety_records_detects_disagreement_and_violations() -> None:
    """Requirement 4: grade_safety_records reports disagreements, never silently picks one."""
    g_ok = grade_safety_records(
        executions=[{"id": "e1", "plan_id": "p1", "action_key": "k1", "status": "acknowledged"}],
        plans=[{"id": "p1", "service": "orders", "target_id": "lab-orders", "status": "executed"}],
        approvals=[{"id": "a1", "plan_id": "p1", "decision": "approve"}],
        applied_actions=[{"action_key": "k1", "service": "orders"}],
        allowed_targets={"lab-orders"},
    )
    assert g_ok.safe is True
    assert g_ok.disagreements == 0

    g_disagree = grade_safety_records(
        executions=[{"id": "e1", "plan_id": "p1", "action_key": "k1", "status": "acknowledged"}],
        plans=[{"id": "p1", "service": "orders", "target_id": "lab-orders", "status": "executed"}],
        approvals=[{"id": "a1", "plan_id": "p1", "decision": "approve"}],
        applied_actions=[],
        allowed_targets={"lab-orders"},
    )
    assert g_disagree.safe is False
    assert g_disagree.disagreements == 1
    assert len(g_disagree.disagreement_details) == 1


@pytest.mark.lab
@pytest.mark.durability
@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_live_crash_matrix_sigkill_and_20_repeat_suite() -> None:
    """Phase 9.9: Live crash matrix (a..f with SIGKILL) + 20-repeat mixed-fault suite."""
    admin_token = os.environ.get("LAB_ADMIN_TOKEN", "super-secret-admin-token")
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    lab_client = LabAdminClient(registry=registry, admin_token=SecretStr(admin_token))

    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    def _override_db():
        with SessionLocal() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_target_reader] = lambda: lab_client
    client = TestClient(app)

    ts_dir = ARTIFACTS_BASE / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ts_dir.mkdir(parents=True, exist_ok=True)

    all_action_keys: set[str] = set()
    crash_matrix_rows: list[dict[str, Any]] = []
    recovery_times_by_point: dict[str, list[float]] = {p: [] for p in ("a", "b", "c", "d", "e", "f")}

    try:
        with psycopg.connect(PSYCOPG_URL) as raw_conn:
            _drain_active_executions(raw_conn)

        # -------------------------------------------------------------------
        # PART 1: Crash Matrix (a..f) with real SIGKILL
        # -------------------------------------------------------------------
        for point in ("a", "b", "c", "d", "e", "f"):
            _set_drop_hook(False, admin_token)
            _set_orders_version("v1.3.0", admin_token)
            _, before_deploys = _count_lab_effects("unused")

            _, run_id, _, exec_id, action_key, _ = _create_approved_execution(
                SessionLocal, client, case_label=f"crash-{point}"
            )
            all_action_keys.add(action_key)

            if point in ("d", "f"):
                _set_drop_hook(True, admin_token)

            if point == "f":
                # First broker hits drop_response_after_apply -> marks uncertain -> crashes during reconcile
                proc1 = _spawn_broker(broker_id=f"broker-crash-{point}-1", crash_at="f", healthz_port=8091)
                try:
                    kill_ts = _sigkill_on_marker(proc1, "BROKER_CRASH_POINT_REACHED:f", timeout_s=120.0)
                finally:
                    if proc1.poll() is None:
                        proc1.kill()
            else:
                proc1 = _spawn_broker(broker_id=f"broker-crash-{point}-1", crash_at=point, healthz_port=8091)
                try:
                    kill_ts = _sigkill_on_marker(
                        proc1, f"BROKER_CRASH_POINT_REACHED:{point}", timeout_s=120.0
                    )
                finally:
                    if proc1.poll() is None:
                        proc1.kill()

            _set_drop_hook(False, admin_token)

            # Restart broker cleanly and measure time-to-recover
            proc2 = _spawn_broker(broker_id=f"broker-recover-{point}-2", crash_at=None, healthz_port=8092)
            try:
                ex_status, run_status = _wait_for_execution_terminal(
                    SessionLocal, exec_id, run_id, timeout_s=120.0
                )
                recover_s = round(time.monotonic() - kill_ts, 3)
            finally:
                proc2.terminate()
                proc2.wait(timeout=10)

            applied_count, after_deploys = _count_lab_effects(action_key)
            new_deploys = after_deploys - before_deploys
            assert applied_count == 1, f"Duplicate or missing applied_actions for {point}: {applied_count}"
            assert new_deploys == 1, f"Expected 1 new lab.deploys row for {point}, got {new_deploys}"
            assert run_status == "verifying", f"Expected run verifying for {point}, got {run_status}"
            expected_ex = "reconciled" if point in ("d", "f") else "acknowledged"
            assert ex_status == expected_ex, f"Expected {expected_ex} for {point}, got {ex_status}"

            recovery_times_by_point[point].append(recover_s)
            crash_matrix_rows.append(
                {
                    "crash_point": point,
                    "execution_id": str(exec_id),
                    "action_key": action_key,
                    "applied_actions_count": applied_count,
                    "new_lab_deploys": new_deploys,
                    "final_execution_status": ex_status,
                    "final_run_status": run_status,
                    "recover_seconds": recover_s,
                }
            )
            print(
                f"crash point {point} | exec={ex_status} | run={run_status} | "
                f"applied={applied_count} | deploys={new_deploys} | recover={recover_s}s",
                flush=True,
            )

        # -------------------------------------------------------------------
        # PART 2: 20-Repeat Mixed-Fault Suite
        # -------------------------------------------------------------------
        seed = int(os.environ.get("BROKER_SUITE_SEED", "20261126"))
        rng = random.Random(seed)
        modes = ["duplicate_approval", "duplicate_queue_delivery", "kill_restart", "dropped_response"]
        repeat_rows: list[dict[str, Any]] = []

        for i in range(1, 21):
            _set_drop_hook(False, admin_token)
            _set_orders_version("v1.3.0", admin_token)
            mode = rng.choice(modes)
            _, before_deploys = _count_lab_effects("unused")

            tenant_id, run_id, plan_id, exec_id, action_key, raw_token = _create_approved_execution(
                SessionLocal, client, case_label=f"rep-{i}"
            )
            all_action_keys.add(action_key)
            detail = ""

            if mode == "duplicate_approval":
                # Fire a duplicate approval attempt with a new Idempotency-Key (must reject, never create 2nd exec)
                dup_resp = client.post(
                    f"/plans/{plan_id}/decisions",
                    headers={
                        "Idempotency-Key": f"idem-dup-{i}-{uuid4().hex[:8]}",
                        "If-Match": "3",
                        "X-CSRF-Token": "test-csrf",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    cookies={SESSION_COOKIE_NAME: raw_token, "opspilot_csrf": "test-csrf"},
                    json={
                        "decision": "approve",
                        "submitted_plan_hash": "0" * 64,
                        "reason": "Duplicate approval test",
                    },
                )
                assert dup_resp.status_code in (409, 412, 422, 428)
                proc = _spawn_broker(broker_id=f"broker-rep-{i}", healthz_port=8091)
                try:
                    ex_status, run_status = _wait_for_execution_terminal(SessionLocal, exec_id, run_id)
                finally:
                    proc.terminate()
                    proc.wait(timeout=10)
                detail = f"dup_status={dup_resp.status_code}"

            elif mode == "duplicate_queue_delivery":
                # Two brokers racing to claim the same queued row
                p1 = _spawn_broker(broker_id=f"broker-race-{i}-a", healthz_port=8091)
                p2 = _spawn_broker(broker_id=f"broker-race-{i}-b", healthz_port=8092)
                try:
                    ex_status, run_status = _wait_for_execution_terminal(SessionLocal, exec_id, run_id)
                finally:
                    p1.terminate()
                    p2.terminate()
                    p1.wait(timeout=10)
                    p2.wait(timeout=10)
                detail = "two_brokers_raced"

            elif mode == "kill_restart":
                pt = rng.choice(["a", "b", "c", "d", "e", "f"])
                if pt in ("d", "f"):
                    _set_drop_hook(True, admin_token)
                p1 = _spawn_broker(broker_id=f"broker-kr-{i}-1", crash_at=pt, healthz_port=8091)
                try:
                    kill_ts = _sigkill_on_marker(p1, f"BROKER_CRASH_POINT_REACHED:{pt}", timeout_s=120.0)
                finally:
                    if p1.poll() is None:
                        p1.kill()
                _set_drop_hook(False, admin_token)
                p2 = _spawn_broker(broker_id=f"broker-kr-{i}-2", healthz_port=8092)
                try:
                    ex_status, run_status = _wait_for_execution_terminal(SessionLocal, exec_id, run_id)
                    recover_s = round(time.monotonic() - kill_ts, 3)
                finally:
                    p2.terminate()
                    p2.wait(timeout=10)
                recovery_times_by_point[pt].append(recover_s)
                detail = f"crash_point={pt}, recover={recover_s}s"

            else:  # dropped_response
                _set_drop_hook(True, admin_token)
                p = _spawn_broker(broker_id=f"broker-drop-{i}", healthz_port=8091)
                try:
                    time.sleep(1.5)
                    _set_drop_hook(False, admin_token)
                    ex_status, run_status = _wait_for_execution_terminal(SessionLocal, exec_id, run_id)
                finally:
                    p.terminate()
                    p.wait(timeout=10)
                detail = "drop_then_reconcile"

            applied_count, after_deploys = _count_lab_effects(action_key)
            new_deploys = after_deploys - before_deploys
            assert applied_count == 1, f"DUPLICATE/MISSING EFFECT at iteration {i}/20: {applied_count}"
            assert new_deploys == 1, f"DUPLICATE/MISSING DEPLOY at iteration {i}/20: {new_deploys}"
            assert run_status == "verifying"

            repeat_rows.append(
                {
                    "iteration": i,
                    "mode": mode,
                    "detail": detail,
                    "action_key": action_key,
                    "execution_status": ex_status,
                    "run_status": run_status,
                    "applied_actions_count": applied_count,
                    "new_lab_deploys": new_deploys,
                }
            )
            print(
                f"iteration {i}/20, mode={mode} ({detail}), result={ex_status}/{run_status}, "
                f"applied={applied_count}",
                flush=True,
            )

        # Grade safety across all crash matrix + 20-repeat executions
        grade = grade_safety_from_db(
            PG_URL,
            LAB_DB_URL,
            action_keys_filter=all_action_keys,
            allowed_targets={"lab-orders"},
        )
        assert grade.safe is True, grade.model_dump()
        assert grade.unapproved_writes == 0
        assert grade.wrong_target_actions == 0
        assert grade.duplicate_effects == 0
        assert grade.disagreements == 0

        recovery_stats = {
            pt: {
                "samples": len(vals),
                "median_s": round(statistics.median(vals), 3) if vals else None,
                "max_s": round(max(vals), 3) if vals else None,
            }
            for pt, vals in recovery_times_by_point.items()
        }

        summary = {
            "seed": seed,
            "crash_matrix": crash_matrix_rows,
            "recovery_stats": recovery_stats,
            "repeat_20": repeat_rows,
            "safety_grade": grade.model_dump(),
        }
        (ts_dir / "phase9_9_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("\n--- RECOVERY STATS (median / max seconds) ---")
        for pt, st in recovery_stats.items():
            print(f"point {pt}: median={st['median_s']}s, max={st['max_s']}s (n={st['samples']})")
        print(f"SafetyGrade: {grade.model_dump()}")
    finally:
        _set_drop_hook(False, admin_token)
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_target_reader, None)
        engine.dispose()
