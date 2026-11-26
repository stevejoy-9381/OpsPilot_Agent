"""Tests for broker/settings.py, broker/checks.py (B1-B10), broker/main.py, and import/dep isolation."""

from __future__ import annotations

import ast
import os
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.models.tables import ActionExecutionModel, ActionPlanModel, RunModel
from broker.checks import Allowed, Refused, run_prechecks
from broker.main import BrokerService
from broker.settings import BrokerSettings
from shared.plans import compute_action_key, plan_hash
from shared.targets import (
    DEFAULT_LAB_TARGETS_PATH,
    TargetState,
    TargetStateError,
    TargetStateErrorCode,
)

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


class FakeTarget:
    """Offline TargetStateReader stub for B8 and fail-closed tests."""

    def __init__(
        self,
        version: str = "v1.3.0",
        *,
        unavailable: bool = False,
        raise_exc: bool = False,
    ) -> None:
        self.version = version
        self.unavailable = unavailable
        self.raise_exc = raise_exc

    def current_state(self, target_id: str, as_of: datetime) -> TargetState | TargetStateError:
        if self.raise_exc:
            raise RuntimeError("Simulated target reader exception")
        if self.unavailable:
            return TargetStateError(
                code=TargetStateErrorCode.STATE_UNAVAILABLE,
                target_id=target_id,
                message="Simulated unavailable target",
            )
        return TargetState(
            target_id=target_id,
            current_version=self.version,
            previous_version="v1.0.0",
            source="fake_target",
            read_at=as_of,
        )


def _seed_case(
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
    override_action_key: str | None = None,
    exec_status: str = "queued",
) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_id = uuid4()
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
            (tenant_id, "Broker Check", f"bc-{tenant_id.hex[:8]}"),
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
        # Bypass plan_hash insert trigger only when testing non-standard target/action
        if target_id == "lab-unknown":
            cur.execute("ALTER TABLE app.action_plans DISABLE TRIGGER trg_action_plans_verify_plan_hash")
        cur.execute(
            """
            INSERT INTO app.action_plans (
                id, run_id, tenant_id, action, service, target_id, expected_version,
                rollback_to_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at
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
                "Re-deploy",
                p_hash,
                plan_status,
                expires_at,
            ),
        )
        if target_id == "lab-unknown":
            cur.execute("ALTER TABLE app.action_plans ENABLE TRIGGER trg_action_plans_verify_plan_hash")

        if create_approval:
            cur.execute(
                """
                INSERT INTO app.approvals (id, plan_id, tenant_id, reviewer_id, decision, submitted_plan_hash, reason)
                VALUES (%s, %s, %s, %s, 'approve', %s, 'ok')
                """,
                (uuid4(), plan_id, tenant_id, user_id, p_hash),
            )
        cur.execute(
            """
            INSERT INTO app.action_executions (id, plan_id, tenant_id, action_key, status, attempts)
            VALUES (%s, %s, %s, %s, %s, 0)
            """,
            (exec_id, plan_id, tenant_id, a_key, exec_status),
        )
    return tenant_id, run_id, plan_id, exec_id


def test_broker_settings_fail_fast(monkeypatch: pytest.MonkeyPatch):
    """BrokerSettings fails fast when required secrets are missing."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LAB_ADMIN_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        BrokerSettings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setenv("LAB_ADMIN_TOKEN", "secret-tok")
    s = BrokerSettings(_env_file=None)
    assert s.database_url.get_secret_value() == "postgresql://u:p@127.0.0.1:5432/db"
    assert s.lab_admin_token.get_secret_value() == "secret-tok"


_FORBIDDEN_PREFIXES = (
    "agent",
    "tools.read",
    "tools",
    "apps.api.routers",
    "google",
    "langgraph",
    "openai",
    "anthropic",
)


def _resolve_local_module(module_name: str) -> Path | None:
    """Resolves a local Python module name to its source file if it exists in the repo."""
    parts = module_name.split(".")
    cand_file = Path(*parts).with_suffix(".py")
    if cand_file.is_file():
        return cand_file
    cand_init = Path(*parts, "__init__.py")
    if cand_init.is_file():
        return cand_init
    return None


def _extract_imports(source: str, filename: str, current_module: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                pkg_parts = current_module.split(".")[: -node.level]
                base = ".".join(pkg_parts)
                mod = f"{base}.{node.module}" if (base and node.module) else (node.module or base)
            else:
                mod = node.module or ""
            if mod:
                imports.append(mod)
                for alias in node.names:
                    if alias.name != "*":
                        imports.append(f"{mod}.{alias.name}")
    return imports


def _find_transitive_forbidden_imports(
    entry_modules: list[str],
    *,
    overrides: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Walks all reachable imports from entry_modules and returns [(from_mod, forbidden_mod)]."""
    visited: set[str] = set()
    queue = list(entry_modules)
    violations: list[tuple[str, str]] = []

    while queue:
        mod = queue.pop(0)
        if mod in visited:
            continue
        visited.add(mod)

        if overrides and mod in overrides:
            src = overrides[mod]
            filename = f"<override:{mod}>"
        else:
            path = _resolve_local_module(mod)
            if path is None:
                continue
            src = path.read_text(encoding="utf-8")
            filename = str(path)

        for imp in _extract_imports(src, filename, mod):
            if imp.startswith(_FORBIDDEN_PREFIXES):
                violations.append((mod, imp))
            elif imp not in visited and (
                (overrides and imp in overrides) or _resolve_local_module(imp) is not None
            ):
                queue.append(imp)

    return violations


def test_broker_import_graph_and_pyproject_extra():
    """Broker transitively imports nothing from agent/, tools/, apps/api/routers, or LLM SDKs."""
    entry_mods = [
        ".".join(p.with_suffix("").parts)
        for p in Path("broker").rglob("*.py")
        if p.name != "__init__.py"
    ]
    assert "broker.main" in entry_mods
    violations = _find_transitive_forbidden_imports(entry_mods)
    assert violations == [], f"Transitive forbidden imports found: {violations}"

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    broker_deps = pyproject["project"]["optional-dependencies"]["broker"]
    joined = " ".join(broker_deps).lower()
    for forbidden_pkg in ("langgraph", "google-genai", "redis", "opentelemetry", "argon2"):
        assert forbidden_pkg not in joined, f"Unexpected pkg '{forbidden_pkg}' in broker extra"


def test_transitive_import_graph_catches_bad_import():
    """Negative test: transitive import checker fails when a bad import is injected."""
    violations = _find_transitive_forbidden_imports(
        ["broker.main"],
        overrides={"broker.checks": "from agent.llm.interface import LLMClient\n"},
    )
    assert any(
        src == "broker.checks" and dst.startswith("agent.llm")
        for src, dst in violations
    ), f"Expected transitive checker to catch injected bad import, got: {violations}"


def _drain_active_executions(cur: psycopg.Cursor) -> None:
    cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
    cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE status = 'dispatched'")
    cur.execute("UPDATE app.action_executions SET status = 'failed' WHERE status IN ('claimed', 'uncertain')")


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_b1_to_b10_refusals_happy_path_and_fail_closed():
    """Reproduces every B1-B10 refusal, happy path Allowed, and fail-closed errors."""
    eng = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=eng, future=True)
    refusal_matrix: list[tuple[str, str, str]] = []

    try:
        with psycopg.connect(PSYCOPG_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                _drain_active_executions(cur)

            # B1: missing execution
            with SessionLocal() as s:
                res = run_prechecks(s, uuid4(), datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "execution_or_plan_missing"
                refusal_matrix.append(("B1", "missing_execution_row", f"Refused({res.code})"))

            # B2: plan not approved
            _, _, _, exec_b2 = _seed_case(conn, plan_status="proposed")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b2, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "plan_not_approved"
                refusal_matrix.append(("B2", "plan_status_proposed", f"Refused({res.code})"))

            # B3a: missing approval
            _, _, _, exec_b3_missing = _seed_case(conn, create_approval=False)
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b3_missing, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "approval_invalid_or_missing"
                refusal_matrix.append(("B3", "missing_approval_row", f"Refused({res.code})"))

            # B3b: non-reviewer role (viewer / service-principal equivalent)
            _, _, _, exec_b3_role = _seed_case(conn, user_role="viewer")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b3_role, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "approval_invalid_or_missing"
                refusal_matrix.append(("B3", "non_reviewer_approver_role", f"Refused({res.code})"))

            # B4: tampered plan content (trigger disabled in test)
            _, _, plan_b4, exec_b4 = _seed_case(conn)
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE app.action_plans DISABLE TRIGGER trg_action_plans_enforce_immutability")
                cur.execute("UPDATE app.action_plans SET expected_version = 'v9.9.9' WHERE id = %s", (plan_b4,))
                cur.execute("ALTER TABLE app.action_plans ENABLE TRIGGER trg_action_plans_enforce_immutability")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b4, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "plan_hash_integrity_failed"
                refusal_matrix.append(("B4", "tampered_plan_content", f"Refused({res.code})"))

            # B5: expired plan
            _, _, _, exec_b5 = _seed_case(conn, ttl_seconds=-60)
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b5, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "execution_expired"
                refusal_matrix.append(("B5", "expired_plan_at_dispatch", f"Refused({res.code})"))

            # B6: action_key mismatch
            _, _, _, exec_b6 = _seed_case(conn, override_action_key=f"wrong-action-key-{uuid4().hex[:8]}")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b6, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "action_key_mismatch"
                refusal_matrix.append(("B6", "action_key_mismatch", f"Refused({res.code})"))

            # B7: target not in registry
            _, _, _, exec_b7 = _seed_case(conn, target_id="lab-unknown")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b7, datetime.now(timezone.utc), FakeTarget(), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "target_or_action_not_allowed"
                refusal_matrix.append(("B7", "unknown_target_id", f"Refused({res.code})"))

            # B8: stale target
            _, _, _, exec_b8 = _seed_case(conn, expected_version="v1.3.0")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b8, datetime.now(timezone.utc), FakeTarget(version="v1.0.0"), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "stale_target"
                refusal_matrix.append(("B8", "live_version_mismatch", f"Refused({res.code})"))

            # B9: per-target lock (another execution for lab-orders is dispatched)
            _, _, _, _ = _seed_case(conn, target_id="lab-orders", exec_status="dispatched")
            _, _, _, exec_b9 = _seed_case(conn, target_id="lab-orders", exec_status="queued")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b9, datetime.now(timezone.utc), FakeTarget(version="v1.3.0"), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "target_locked"
                refusal_matrix.append(("B9", "inflight_execution_on_target", f"Refused({res.code})"))
            with conn.cursor() as cur:
                _drain_active_executions(cur)

            # B10: run not approved
            _, _, _, exec_b10 = _seed_case(conn, run_status="needs_review")
            with SessionLocal() as s:
                res = run_prechecks(s, exec_b10, datetime.now(timezone.utc), FakeTarget(version="v1.3.0"), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res, Refused) and res.code == "run_not_approved_or_stale"
                refusal_matrix.append(("B10", "run_status_needs_review", f"Refused({res.code})"))

            # Fail-closed: reader down, reader exception, missing registry
            _, _, _, exec_ok = _seed_case(conn)
            with SessionLocal() as s:
                r_down = run_prechecks(s, exec_ok, datetime.now(timezone.utc), FakeTarget(unavailable=True), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(r_down, Refused) and r_down.code == "policy_unavailable"
                refusal_matrix.append(("FC", "target_reader_unavailable", f"Refused({r_down.code})"))

                r_exc = run_prechecks(s, exec_ok, datetime.now(timezone.utc), FakeTarget(raise_exc=True), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(r_exc, Refused) and r_exc.code == "policy_unavailable"
                refusal_matrix.append(("FC", "target_reader_exception", f"Refused({r_exc.code})"))

                r_reg = run_prechecks(s, exec_ok, datetime.now(timezone.utc), FakeTarget(), Path("nonexistent_targets.yaml"))
                assert isinstance(r_reg, Refused) and r_reg.code == "policy_unavailable"
                refusal_matrix.append(("FC", "missing_target_registry", f"Refused({r_reg.code})"))

            # Happy path: Allowed
            with SessionLocal() as s:
                res_ok = run_prechecks(s, exec_ok, datetime.now(timezone.utc), FakeTarget(version="v1.3.0"), DEFAULT_LAB_TARGETS_PATH)
                assert isinstance(res_ok, Allowed)
                refusal_matrix.append(("ALL", "valid_approved_execution", "Allowed"))

            # BrokerService.process_next() end-to-end refusal handling (expired plan -> refused, plan=expired, run=escalated)
            with conn.cursor() as cur:
                _drain_active_executions(cur)
            t_exp, run_exp, plan_exp, exec_exp = _seed_case(conn, ttl_seconds=-30)
            svc = BrokerService(
                session_factory=SessionLocal,
                target_reader=FakeTarget(version="v1.3.0"),
                target_registry=DEFAULT_LAB_TARGETS_PATH,
                broker_id="broker-test",
            )
            assert svc.process_next() == "refused"

            with SessionLocal() as s:
                s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(t_exp)})
                ex_row = s.get(ActionExecutionModel, exec_exp)
                pl_row = s.get(ActionPlanModel, plan_exp)
                rn_row = s.get(RunModel, run_exp)
                assert ex_row is not None and ex_row.status == "refused" and ex_row.refusal_code == "execution_expired"
                assert pl_row is not None and pl_row.status == "expired"
                assert rn_row is not None and rn_row.status == "escalated"
                assert rn_row.last_error is not None and rn_row.last_error["code"] == "execution_expired"

        print("\n--- REFUSAL MATRIX ---")
        for chk, scen, out in refusal_matrix:
            print(f"{chk:<4} | {scen:<30} | {out}")
    finally:
        eng.dispose()
