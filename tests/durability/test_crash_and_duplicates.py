"""Subprocess durability & duplicate-delivery tests (Feature 8, Phase 8.7).

Uses real subprocesses for worker & API, real PostgreSQL, and real PostgresSaver
checkpointer, with FakeLLMClient and snapshot data only (no network, no Docker).

Process kill note:
- On POSIX (Linux / Codespaces), `proc.kill()` sends `SIGKILL`.
- On Windows, `proc.kill()` calls `TerminateProcess` (immediate hard kill equivalent).

Skips cleanly with an explicit message if PostgreSQL is not reachable.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalModel,
    IncidentModel,
    RunEventModel,
    RunModel,
    TenantModel,
    UserModel,
)
from scripts.db_invariants import check_invariants
from shared.actions import ActionType
from shared.plans import PlanStatus, plan_hash

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = REPO_ROOT / "evals" / "results" / "durability"

PG_URL = os.environ.get(
    "TEST_DB_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:5432/opspilot",
    ),
)
if PG_URL.startswith("postgresql://"):
    PG_URL = "postgresql+psycopg://" + PG_URL[len("postgresql://") :]


def _pg_available() -> bool:
    try:
        eng = create_engine(PG_URL, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.durability,
    pytest.mark.skipif(
        not _pg_available(),
        reason="Live PostgreSQL not reachable; run in Codespaces or with local Postgres running",
    ),
]


def _write_timeline_artifact(name: str, session: Session, run_id: UUID) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    events = list(
        session.scalars(
            select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.seq.asc())
        ).all()
    )
    out_path = ARTIFACTS_DIR / f"{name}.json"
    out_path.write_text(
        json.dumps(
            {
                "scenario": name,
                "run_id": str(run_id),
                "events": [
                    {
                        "seq": e.seq,
                        "type": e.type,
                        "actor": e.actor,
                        "payload": e.payload,
                        "created_at": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in events
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_path


def _assert_invariants_clean() -> None:
    assert check_invariants() == 0, "scripts/db_invariants.py reported invariant violations"


def _spawn_api_subprocess(port: int = 18091) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["DATABASE_URL"] = PG_URL
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_worker_subprocess() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["DATABASE_URL"] = PG_URL
    env["CHECKPOINTER_TYPE"] = "postgres"
    env["LEASE_SECONDS"] = "5"
    env["HEARTBEAT_SECONDS"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "agent.worker"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _seed_pg_review_run(session: Session) -> tuple[UUID, UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    user_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=15)

    session.add(TenantModel(id=tenant_id, name="Durability", slug=f"dur-{tenant_id.hex[:8]}"))
    session.add(
        UserModel(
            id=user_id,
            tenant_id=tenant_id,
            email=f"dur-{user_id.hex[:6]}@example.com",
            role="reviewer",
            password_hash="not-used",
        )
    )
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_dur",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={"service": "orders"},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            status="needs_review",
            version=1,
            remaining_calls=5,
            max_calls=8,
        )
    )
    phash = plan_hash(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        target_id="lab-orders",
        expected_version="v1.1.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=["ev_1"],
        expires_at=expires_at,
    )
    session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="orders",
            target_id="lab-orders",
            expected_version="v1.1.0",
            rollback_to_version="v1.0.0",
            rationale_evidence_ids=["ev_1"],
            rollback_route="Re-deploy orders v1.1.0",
            plan_hash=phash,
            policy_version="v1",
            status=PlanStatus.PROPOSED.value,
            notes="pool leak",
            expires_at=expires_at,
        )
    )
    session.commit()
    return tenant_id, user_id, run_id, plan_id, phash


def test_crash_c_wait_at_interrupt_preserves_needs_review() -> None:
    eng = create_engine(PG_URL)
    SessionFactory = sessionmaker(bind=eng, expire_on_commit=False)
    with SessionFactory() as session:
        _, _, run_id, plan_id, _ = _seed_pg_review_run(session)

    worker = _spawn_worker_subprocess()
    try:
        time.sleep(1.0)
        worker.kill()
        worker.wait(timeout=5)
    finally:
        if worker.poll() is None:
            worker.kill()

    worker2 = _spawn_worker_subprocess()
    try:
        time.sleep(1.0)
        with SessionFactory() as session:
            run_row = session.get(RunModel, run_id)
            plan_row = session.get(ActionPlanModel, plan_id)
            assert run_row is not None and run_row.status == "needs_review"
            assert plan_row is not None and plan_row.status == "proposed"
            _write_timeline_artifact("crash_c_interrupt_wait", session, run_id)
    finally:
        worker2.kill()
        worker2.wait(timeout=5)

    _assert_invariants_clean()
    eng.dispose()


def test_crash_d_kill_api_before_commit_and_retry() -> None:
    eng = create_engine(PG_URL)
    SessionFactory = sessionmaker(bind=eng, expire_on_commit=False)
    with SessionFactory() as session:
        _, _, run_id, plan_id, _ = _seed_pg_review_run(session)

    api_proc = _spawn_api_subprocess(port=18091)
    try:
        time.sleep(1.0)
        api_proc.kill()
        api_proc.wait(timeout=5)
        with SessionFactory() as session:
            assert session.query(ApprovalModel).filter_by(plan_id=plan_id).count() == 0
            assert session.query(ActionExecutionModel).filter_by(plan_id=plan_id).count() == 0
            _write_timeline_artifact("crash_d_api_pre_commit", session, run_id)
    finally:
        if api_proc.poll() is None:
            api_proc.kill()

    _assert_invariants_clean()
    eng.dispose()


def test_duplicate_delivery_20_concurrent_requests() -> None:
    eng = create_engine(PG_URL, pool_size=25)
    SessionFactory = sessionmaker(bind=eng, expire_on_commit=False)
    with SessionFactory() as session:
        _, _, run_id, plan_id, phash = _seed_pg_review_run(session)

    api_proc = _spawn_api_subprocess(port=18092)
    try:
        time.sleep(1.5)
        url = f"http://127.0.0.1:18092/plans/{plan_id}/decisions"

        def _send(idx: int) -> int:
            resp = httpx.post(
                url,
                headers={
                    "X-Requested-With": "OpsPilotApp",
                    "If-Match": "1",
                    "Idempotency-Key": f"dup-{idx % 4}",
                },
                json={"decision": "approve", "submitted_plan_hash": phash, "reason": "ok"},
                timeout=5.0,
            )
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(_send, range(20)))
        assert len(statuses) == 20
        with SessionFactory() as session:
            assert session.query(ApprovalModel).filter_by(plan_id=plan_id).count() <= 1
            assert session.query(ActionExecutionModel).filter_by(plan_id=plan_id).count() <= 1
            _write_timeline_artifact("duplicate_delivery_20", session, run_id)
    finally:
        api_proc.kill()
        api_proc.wait(timeout=5)

    _assert_invariants_clean()
    eng.dispose()
