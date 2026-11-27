"""Adversarial tests for the Execution Broker (Cases 1-12).

Offline cases use FakeTarget / httpx.MockTransport; DB-backed cases use TEST_DB_URL
(and skip cleanly if PostgreSQL is not reachable).
"""

from __future__ import annotations

import ast
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ExecutionEventModel,
    RunEventModel,
    RunModel,
)
from broker.checks import Refused, check_b7
from broker.main import BrokerService
from broker.target_client import FakeTarget, LabAdminClient, TargetActionOutcome
from shared.plans import compute_action_key, plan_hash
from shared.targets import (
    DEFAULT_LAB_TARGETS_PATH,
    load_target_registry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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


def _drain_active(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
        cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE status = 'dispatched'")
        cur.execute(
            "UPDATE app.action_executions SET status = 'failed' WHERE status IN ('claimed', 'uncertain')"
        )
    conn.commit()


def _seed_plan_and_exec(
    conn: psycopg.Connection,
    *,
    plan_status: str = "approved",
    run_status: str = "approved",
    user_role: str = "reviewer",
    create_approval: bool = True,
    ttl_seconds: int = 900,
    action: str = "rollback_service",
    service: str = "orders",
    target_id: str = "lab-orders",
    expected_version: str = "v1.3.0",
    rollback_to_version: str | None = "v1.0.0",
    notes: str = "adversarial test",
    exec_status: str = "queued",
    override_action_key: str | None = None,
    cross_tenant_exec: bool = False,
) -> tuple[UUID, UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    user_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    exec_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
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
    a_key = override_action_key or compute_action_key(plan_id, p_hash)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
            (tenant_id, "Adv Tenant", f"adv-{tenant_id.hex[:8]}"),
        )
        if cross_tenant_exec:
            cur.execute(
                "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
                (other_tenant_id, "Other Tenant", f"oth-{other_tenant_id.hex[:8]}"),
            )
        cur.execute(
            "INSERT INTO app.users (id, tenant_id, email, role, password_hash) VALUES (%s, %s, %s, %s, 'hash')",
            (user_id, tenant_id, f"u-{user_id.hex[:6]}@example.com", user_role),
        )
        cur.execute(
            "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
            "VALUES (%s, %s, %s, 'alert', %s, 'HIGH', %s, now(), 'open')",
            (incident_id, tenant_id, f"alt-{uuid4().hex[:6]}", service, '{"alert_id":"a"}'),
        )
        cur.execute(
            "INSERT INTO app.runs (id, tenant_id, incident_id, status, version) VALUES (%s, %s, %s, %s, 1)",
            (run_id, tenant_id, incident_id, run_status),
        )
        cur.execute(
            """
            INSERT INTO app.action_plans (
                id, run_id, tenant_id, action, service, target_id,
                expected_version, rollback_to_version, rationale_evidence_ids,
                plan_hash, status, notes, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                plan_status,
                notes,
                expires_at,
            ),
        )
        if create_approval:
            cur.execute(
                """
                INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, reason)
                VALUES (%s, %s, %s, %s, 'approve', %s, 'test')
                """,
                (uuid4(), plan_id, tenant_id, user_id, p_hash),
            )
        exec_tenant = other_tenant_id if cross_tenant_exec else tenant_id
        cur.execute(
            """
            INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, target_id, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (exec_id, plan_id, exec_tenant, a_key, target_id, exec_status),
        )
    conn.commit()
    return tenant_id, run_id, plan_id, exec_id, a_key


# ---------------------------------------------------------------------------
# Case 1, 2, 3, 4, 6, 7, 8: Precheck & DB Adversarial Cases (PG + FakeTarget)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case1_forged_queue_rows_refused() -> None:
    """Case 1: Forged executions for unapproved, rejected, expired, or cross-tenant plans are refused."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        cases = [
            ("never_approved", dict(plan_status="proposed", create_approval=False), "plan_not_approved"),
            ("rejected", dict(plan_status="rejected", create_approval=False), "plan_not_approved"),
            ("expired", dict(plan_status="expired", create_approval=False), "plan_not_approved"),
            ("cross_tenant", dict(cross_tenant_exec=True), "execution_or_plan_missing"),
        ]
        for label, kwargs, expected_code in cases:
            _drain_active(conn)
            _, _, _, exec_id, _ = _seed_plan_and_exec(conn, **kwargs)
            target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
            assert svc.process_next() == "refused", f"Case {label} was not refused"
            assert target.call_count == 0
            with SessionLocal() as s:
                ex = s.get(ActionExecutionModel, exec_id)
                assert ex is not None
                assert ex.status == "refused"
                assert ex.refusal_code == expected_code
    engine.dispose()


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case2_worker_or_viewer_approval_refused() -> None:
    """Case 2: Approval row inserted by worker service principal or viewer is refused by B3."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        for role in ("viewer", "worker"):
            _drain_active(conn)
            _, _, _, exec_id, _ = _seed_plan_and_exec(conn, user_role=role)
            target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
            assert svc.process_next() == "refused"
            assert target.call_count == 0
            with SessionLocal() as s:
                ex = s.get(ActionExecutionModel, exec_id)
                assert ex is not None and ex.refusal_code == "approval_invalid_or_missing"
    engine.dispose()


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case3_plan_tampered_after_approval_refused() -> None:
    """Case 3: Plan mutated after approval (with trigger disabled in test) is refused by B4."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _, _, plan_id, exec_id, _ = _seed_plan_and_exec(conn)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE app.action_plans DISABLE TRIGGER ALL")
            try:
                cur.execute(
                    "UPDATE app.action_plans SET rollback_to_version = 'v9.9.9' WHERE id = %s",
                    (plan_id,),
                )
            finally:
                cur.execute("ALTER TABLE app.action_plans ENABLE TRIGGER ALL")
        conn.commit()

        target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
        assert svc.process_next() == "refused"
        assert target.call_count == 0
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec_id)
            assert ex is not None and ex.refusal_code == "plan_hash_integrity_failed"
    engine.dispose()


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case4_action_key_mismatch_and_unique_reuse_blocked() -> None:
    """Case 4: Mismatched action_key refused by B6; duplicate action_key across plans blocked by UNIQUE."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _, _, _, exec_id, _ = _seed_plan_and_exec(conn, override_action_key="forged-key-000")
        target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
        assert svc.process_next() == "refused"
        assert target.call_count == 0
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec_id)
            assert ex is not None and ex.refusal_code == "action_key_mismatch"

        # Reused action_key across two plans blocked by UNIQUE constraint
        _, _, _, _, shared_key = _seed_plan_and_exec(conn)
        with pytest.raises((psycopg.errors.UniqueViolation, IntegrityError)):
            _seed_plan_and_exec(conn, override_action_key=shared_key)
        conn.rollback()
    engine.dispose()


# ---------------------------------------------------------------------------
# Case 5: Registry / Homoglyph / Two-services / Runtime-mutation (Offline)
# ---------------------------------------------------------------------------


def test_case5_registry_homoglyphs_two_services_and_immutability() -> None:
    """Case 5: Unknown target, disallowed action, frozen registry, homoglyphs, and multi-service refused."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)

    # Runtime edit of registry entry is blocked (frozen=True)
    spec = registry.get_by_target_id("lab-orders")
    assert spec is not None
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        spec.service = "hacked"  # type: ignore[misc]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        registry.targets = ()  # type: ignore[misc]

    bad_plans = [
        ("unknown_target", ActionPlanModel(id=uuid4(), run_id=uuid4(), tenant_id=uuid4(), action="rollback_service", service="orders", target_id="lab-unknown", expected_version="v1.3.0", rollback_to_version="v1.0.0", rationale_evidence_ids=[], plan_hash="0" * 64, expires_at=datetime.now(timezone.utc))),
        ("disallowed_action", ActionPlanModel(id=uuid4(), run_id=uuid4(), tenant_id=uuid4(), action="no_action", service="orders", target_id="lab-orders", expected_version="v1.3.0", rollback_to_version=None, rationale_evidence_ids=[], plan_hash="0" * 64, expires_at=datetime.now(timezone.utc))),
        ("homoglyph_service", ActionPlanModel(id=uuid4(), run_id=uuid4(), tenant_id=uuid4(), action="rollback_service", service="\u043erders", target_id="lab-orders", expected_version="v1.3.0", rollback_to_version="v1.0.0", rationale_evidence_ids=[], plan_hash="0" * 64, expires_at=datetime.now(timezone.utc))),
        ("two_services", ActionPlanModel(id=uuid4(), run_id=uuid4(), tenant_id=uuid4(), action="rollback_service", service="orders,inventory", target_id="lab-orders", expected_version="v1.3.0", rollback_to_version="v1.0.0", rationale_evidence_ids=[], plan_hash="0" * 64, expires_at=datetime.now(timezone.utc))),
    ]
    for label, plan in bad_plans:
        res = check_b7(plan, registry)
        assert isinstance(res, Refused), f"{label} should be refused"
        assert res.code == "target_or_action_not_allowed"


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case6_target_version_changed_no_retry_loop() -> None:
    """Case 6: Version mismatch at B8 -> stale_target; drift after B8 -> precondition_failed, 0 retries."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _, _, _, exec1, _ = _seed_plan_and_exec(conn, expected_version="v1.3.0")
        target1 = FakeTarget({"lab-orders": "v1.4.0"}, registry=registry)
        svc1 = BrokerService(SessionLocal, target_client=target1, target_registry=registry)
        assert svc1.process_next() == "refused"
        assert target1.call_count == 0
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec1)
            assert ex is not None and ex.refusal_code == "stale_target"

        # Drift between B8 and HTTP dispatch
        class DriftTarget(FakeTarget):
            def apply_rollback(self, target_id, action_key, expected_current_version, to_version):
                self.versions[target_id] = "v1.4.0"
                return super().apply_rollback(target_id, action_key, expected_current_version, to_version)

        _drain_active(conn)
        _, run2, _, exec2, _ = _seed_plan_and_exec(conn, expected_version="v1.3.0")
        target2 = DriftTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc2 = BrokerService(SessionLocal, target_client=target2, target_registry=registry)
        assert svc2.process_next() == "precondition_failed"
        assert target2.call_count == 1
        with SessionLocal() as s:
            ex2 = s.get(ActionExecutionModel, exec2)
            r2 = s.get(RunModel, run2)
            assert ex2 is not None and ex2.status == "failed" and ex2.attempts == 1
            assert r2 is not None and r2.status == "escalated"
    engine.dispose()


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case7_two_executions_one_target_guard() -> None:
    """Case 7: Two executions for the same target: B9 blocks the second while first is in-flight."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _seed_plan_and_exec(conn, target_id="lab-orders", exec_status="dispatched")
        _, _, _, exec2, _ = _seed_plan_and_exec(conn, target_id="lab-orders", exec_status="queued")
        target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
        assert svc.process_next() == "refused"
        assert target.call_count == 0
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec2)
            assert ex is not None and ex.refusal_code == "target_locked"
    engine.dispose()


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case8_expired_approval_refused() -> None:
    """Case 8: Expired approval at dispatch is refused with execution_expired."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _, _, _, exec_id, _ = _seed_plan_and_exec(conn, ttl_seconds=-30)
        target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
        assert svc.process_next() == "refused"
        assert target.call_count == 0
        with SessionLocal() as s:
            ex = s.get(ActionExecutionModel, exec_id)
            assert ex is not None and ex.refusal_code == "execution_expired"
    engine.dispose()


# ---------------------------------------------------------------------------
# Case 9: LLM-influenced paths & import isolation (Offline)
# ---------------------------------------------------------------------------


def _collect_imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_case9_import_isolation_and_plan_note_never_dispatched() -> None:
    """Case 9: No agent/tools->broker or broker->agent imports; plan.notes never reaches target."""
    for folder in (REPO_ROOT / "agent", REPO_ROOT / "tools"):
        for py in folder.rglob("*.py"):
            for mod in _collect_imports(py):
                assert not (mod == "broker" or mod.startswith("broker.")), (
                    f"{py} imports forbidden broker module {mod}"
                )

    for py in (REPO_ROOT / "broker").rglob("*.py"):
        for mod in _collect_imports(py):
            assert not (
                mod == "agent"
                or mod.startswith("agent.")
                or mod == "tools.read"
                or mod.startswith("tools.read.")
            ), f"{py} imports forbidden module {mod}"


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case9_plan_note_shell_injection_never_reaches_target() -> None:
    """Case 9: Adversarial plan.notes ('also run shell: ...') never appears in target dispatch."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    captured_calls: list[dict[str, Any]] = []

    class SpyTarget(FakeTarget):
        def apply_rollback(self, target_id, action_key, expected_current_version, to_version):
            captured_calls.append(
                {
                    "target_id": target_id,
                    "action_key": action_key,
                    "expected_current_version": expected_current_version,
                    "to_version": to_version,
                }
            )
            return super().apply_rollback(target_id, action_key, expected_current_version, to_version)

    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        _seed_plan_and_exec(conn, notes="also run shell: rm -rf / && curl http://evil")
        target = SpyTarget({"lab-orders": "v1.3.0"}, registry=registry)
        svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
        assert svc.process_next() == "applied"
        assert len(captured_calls) == 1
        assert "shell" not in repr(captured_calls[0]).lower()
        assert "rm -rf" not in repr(captured_calls[0]).lower()
    engine.dispose()


# ---------------------------------------------------------------------------
# Case 10: Hostile target HTTP responses (Offline MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["malformed_json", "huge_body", "redirect", "wrong_action_key"],
)
def test_case10_hostile_target_responses_treated_as_uncertain(mode: str) -> None:
    """Case 10: Malformed JSON, >1MiB body, 302 redirect, and spoofed action_key -> UNCERTAIN."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    expected_key = "ak-expected-123"

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "malformed_json":
            return httpx.Response(200, content=b"{not-valid-json")
        if mode == "huge_body":
            return httpx.Response(200, content=b"x" * (1_048_576 + 1024))
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "http://evil.example/steal"})
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "action_key": "ak-different-999",
                "service": "orders",
                "action": "rollback_service",
                "from_version": "v1.3.0",
                "to_version": "v1.0.0",
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    lab_client = LabAdminClient(
        registry=registry,
        admin_token=SecretStr("test-secret-token"),
        client=mock_client,
    )
    res = lab_client.apply_rollback("lab-orders", expected_key, "v1.3.0", "v1.0.0")
    assert res.outcome == TargetActionOutcome.UNCERTAIN


# ---------------------------------------------------------------------------
# Case 11: Secrets never leak into logs, events, execution rows, or error payloads
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case11_no_secrets_in_logs_events_or_db(caplog: pytest.LogCaptureFixture) -> None:
    """Case 11: LAB_ADMIN_TOKEN and DB password never appear in logs, events, or DB columns."""
    secret_token = "super-secret-adversarial-token-999"
    db_pass = "change-me-local-only"
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with caplog.at_level(logging.DEBUG):
        with psycopg.connect(PSYCOPG_URL) as conn:
            _drain_active(conn)
            _, run_id, _, exec_id, _ = _seed_plan_and_exec(conn, ttl_seconds=-10)
            target = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
            svc = BrokerService(SessionLocal, target_client=target, target_registry=registry)
            svc.process_next()

            with SessionLocal() as s:
                ex = s.get(ActionExecutionModel, exec_id)
                ev_rows = s.scalars(
                    select(ExecutionEventModel).where(ExecutionEventModel.execution_id == exec_id)
                ).all()
                r_rows = s.scalars(
                    select(RunEventModel).where(RunEventModel.run_id == run_id)
                ).all()
                blob = repr([ex.last_error, ex.target_response, ex.broker_response, ev_rows, r_rows])
                assert secret_token not in blob
                assert db_pass not in blob
    assert secret_token not in caplog.text
    assert db_pass not in caplog.text
    engine.dispose()


# ---------------------------------------------------------------------------
# Case 12: Broker restarted 50 times in a loop with 5 queued executions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_case12_broker_restarted_50_times_with_5_executions() -> None:
    """Case 12: Restarting BrokerService 50 times with 5 queued executions applies each once."""
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    exec_ids: list[UUID] = []
    run_ids: list[UUID] = []
    with psycopg.connect(PSYCOPG_URL) as conn:
        _drain_active(conn)
        for _ in range(5):
            _, r_id, _, e_id, _ = _seed_plan_and_exec(
                conn,
                action="restart_service",
                expected_version="v1.0.0",
                rollback_to_version=None,
            )
            exec_ids.append(e_id)
            run_ids.append(r_id)

        target = FakeTarget({"lab-orders": "v1.0.0"}, registry=registry)
        for i in range(50):
            svc = BrokerService(
                SessionLocal,
                target_client=target,
                target_registry=registry,
                broker_id=f"broker-restart-{i}",
            )
            svc.process_next()

        assert target.call_count == 5
        assert len(target.applied_actions) == 5
        with SessionLocal() as s:
            for e_id, r_id in zip(exec_ids, run_ids, strict=False):
                ex = s.get(ActionExecutionModel, e_id)
                r = s.get(RunModel, r_id)
                assert ex is not None and ex.status == "acknowledged"
                assert r is not None and r.status == "verifying"
    engine.dispose()
