"""End-to-end tests connecting Feature 8 approved plans to Feature 9 Execution Broker.

Covers:
1. Offline E2E (FakeTarget):
   - S1: diagnosis -> plan -> approve -> worker await_execution -> broker rollback once -> verifying
   - S3: diagnosis -> plan -> approve -> worker await_execution -> broker restart once -> verifying
   - S5: false alarm -> completed with 0 plans and 0 executions
   - Reviewer audit trail (time, who, what, evidence) with B1-B10 checks and inputs
   - API GET /runs/{id} execution fields, GET /runs/{id}/events sanitized timeline, no execution mutation routes
   - db_invariants.py checks (including stale dispatched, multi non-terminal per target, unapproved execution)
2. Live E2E (@pytest.mark.lab, Codespaces):
   - Real rollback of orders from v1.3.0 -> v1.0.0 via LabAdminClient / live broker
   - Visible new row in lab.deploys
   - db_invariants.py --live (including lab.applied_actions parity)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from agent.graph.nodes.await_review import DatabaseExecutionPort
from agent.graph.state import InvestigationState
from apps.api.auth.dependencies import SESSION_COOKIE_NAME, get_db_session
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.domain.context import TenantContext
from apps.api.main import app
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    DiagnosisModel,
    IncidentModel,
    SessionModel,
    TenantModel,
    UserModel,
)
from apps.api.services.run_service import RunService
from broker.main import BrokerService
from broker.target_client import FakeTarget, LabAdminClient
from scripts.db_invariants import check_invariants
from shared.plans import plan_hash
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


def _drain_active_executions(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE app.action_executions SET status = 'claimed' WHERE status = 'queued'")
        cur.execute("UPDATE app.action_executions SET status = 'uncertain' WHERE status = 'dispatched'")
        cur.execute(
            "UPDATE app.action_executions SET status = 'failed' WHERE status IN ('claimed', 'uncertain')"
        )
    conn.commit()


def _setup_tenant_and_reviewer(
    session: Session,
) -> tuple[UUID, UUID, str]:
    tenant_id = uuid4()
    user_id = uuid4()
    session.add(
        TenantModel(id=tenant_id, name="E2E Tenant", slug=f"e2e-{tenant_id.hex[:8]}")
    )
    session.add(
        UserModel(
            id=user_id,
            tenant_id=tenant_id,
            email=f"reviewer-{user_id.hex[:6]}@example.com",
            role="reviewer",
            password_hash="hashed-pw",
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
    session.commit()
    return tenant_id, user_id, raw_token


def _seed_needs_review_run(
    session: Session,
    *,
    tenant_id: UUID,
    case_id: str,
    service: str,
    target_id: str,
    action: str,
    expected_version: str,
    rollback_to_version: str | None,
) -> tuple[UUID, UUID, UUID, str]:
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
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

    session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id=f"alert-{uuid4().hex[:8]}",
            source="seeded_case",
            service=service,
            severity="HIGH",
            alert_payload={"case_id": case_id, "service": service},
            opened_at=datetime.now(timezone.utc),
            status_summary="open",
        )
    )
    session.flush()

    ctx = TenantContext(tenant_id=tenant_id, role="worker")
    run_svc = RunService(session)
    run = run_svc.create_run(ctx=ctx, incident_id=incident_id, case_id=case_id)
    # Replace with explicit run_id? Use created run.id and recompute plan_hash
    run_id = run.id
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

    run, _ = run_svc.transition(
        ctx=ctx, run_id=run_id, to_status="investigating", actor="worker", expected_version=run.version
    )
    session.add(
        DiagnosisModel(
            run_id=run_id,
            tenant_id=tenant_id,
            root_cause_service=service,
            root_cause_category="bad_deploy" if action == "rollback_service" else "dependency_failure",
            mechanism="connection_pool_exhaustion" if action == "rollback_service" else "dependency_timeout",
            evidence_ids=[ev_id],
            confidence=0.92,
            outcome_kind="diagnosed",
            outcome_reason="concluded_by_model",
        )
    )
    session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action=action,
            service=service,
            target_id=target_id,
            expected_version=expected_version,
            rollback_to_version=rollback_to_version,
            rationale_evidence_ids=[ev_id],
            model_note=f"Automated {action} plan for {case_id}",
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
        payload={"plan_id": str(plan_id), "plan_hash": p_hash},
    )
    session.commit()
    return incident_id, run_id, plan_id, p_hash


def _run_graph_through_await_execution(
    session: Session,
    *,
    tenant_id: UUID,
    run_id: UUID,
    plan_id: UUID,
    plan_hash_val: str,
    action_execution_id: str,
) -> dict[str, Any]:
    """Builds minimal graph at await_review -> interrupts -> resumes -> await_execution -> END."""
    from langgraph.graph import END, StateGraph

    from agent.graph.nodes.await_review import await_execution_node, await_review_node

    exec_port = DatabaseExecutionPort(session)
    builder = StateGraph(InvestigationState)
    builder.add_node("await_review", await_review_node)
    builder.add_node(
        "await_execution",
        lambda s: await_execution_node(s, execution_port=exec_port),
    )
    builder.set_entry_point("await_review")
    builder.add_edge("await_review", "await_execution")
    builder.add_edge("await_execution", END)
    graph = builder.compile(checkpointer=MemorySaver())

    cfg = {"configurable": {"thread_id": str(run_id)}}
    initial: InvestigationState = {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "proposed_plan_id": str(plan_id),
        "proposed_plan_hash": plan_hash_val,
        "status": "needs_review",
    }
    first = graph.invoke(initial, config=cfg)
    assert "__interrupt__" in first

    resumed = graph.invoke(
        Command(resume={"outcome": "approved", "action_execution_id": action_execution_id}),
        config=cfg,
    )
    return resumed


def test_no_endpoint_can_modify_execution() -> None:
    """Requirement 2: No API endpoint exposes POST/PUT/PATCH/DELETE to modify an execution."""
    mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
    bad_routes: list[tuple[str, set[str]]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        if "execution" in path.lower() and (methods & mutating_methods):
            bad_routes.append((path, methods & mutating_methods))
    assert bad_routes == [], f"Unexpected mutating execution routes: {bad_routes}"


@contextmanager
def _override_db_session(session_factory: sessionmaker[Session]):
    def _override_db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_offline_e2e_s1_s3_s5_and_audit_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 1, 2, 3, 5, 6: Offline E2E (FakeTarget) for S1, S3, S5 + timeline + invariants."""
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)

    with psycopg.connect(PSYCOPG_URL) as raw_conn:
        _drain_active_executions(raw_conn)

    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    client = TestClient(app)

    with _override_db_session(SessionLocal), SessionLocal() as session:
        tenant_id, _, token = _setup_tenant_and_reviewer(session)
        client.cookies.set(SESSION_COOKIE_NAME, token)

        # -------------------------------------------------------------------
        # S1: diagnosis -> plan -> approve -> broker executes rollback once -> verifying
        # -------------------------------------------------------------------
        _, s1_run_id, s1_plan_id, s1_hash = _seed_needs_review_run(
            session,
            tenant_id=tenant_id,
            case_id="S1",
            service="orders",
            target_id="lab-orders",
            action="rollback_service",
            expected_version="v1.3.0",
            rollback_to_version="v1.0.0",
        )

        # Approve via API (inserts action_executions(queued) in DB; no direct broker call)
        approve_resp = client.post(
            f"/plans/{s1_plan_id}/decisions",
            headers={
                "Idempotency-Key": f"idem-s1-{uuid4().hex[:8]}",
                "If-Match": s1_hash,
                "X-CSRF-Token": "test-csrf",
            },
            cookies={SESSION_COOKIE_NAME: token, "opspilot_csrf": "test-csrf"},
            json={
                "decision": "approve",
                "submitted_plan_hash": s1_hash,
                "reason": "S1 rollback approved after diagnosis",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text
        approve_data = approve_resp.json()
        s1_exec_id = approve_data["action_execution_id"]
        assert s1_exec_id is not None
        assert approve_data["run_status"] == "approved"

        # Worker resumes graph through await_execution using DatabaseExecutionPort
        resumed = _run_graph_through_await_execution(
            session,
            tenant_id=tenant_id,
            run_id=s1_run_id,
            plan_id=s1_plan_id,
            plan_hash_val=s1_hash,
            action_execution_id=s1_exec_id,
        )
        assert resumed["status"] == "approved"

        # Broker consumes queued execution from DB and executes rollback on FakeTarget once
        fake_s1 = FakeTarget({"lab-orders": "v1.3.0"}, registry=registry)
        broker_s1 = BrokerService(
            session_factory=SessionLocal,
            target_client=fake_s1,
            target_registry=registry,
            broker_id="broker-offline-s1",
        )
        outcome_s1 = broker_s1.process_next()
        assert outcome_s1 == "applied"
        assert fake_s1.call_count == 1
        assert fake_s1.versions["lab-orders"] == "v1.0.0"

        # GET /runs/{id} exposes execution status, attempts, refusal_code, before/after versions, audit trail
        run_resp = client.get(f"/runs/{s1_run_id}")
        assert run_resp.status_code == 200, run_resp.text
        run_data = run_resp.json()
        assert run_data["status"] == "verifying"
        assert run_data["execution_status"] == "acknowledged"
        assert run_data["execution_attempts"] == 1
        assert run_data["execution_refusal_code"] is None
        assert run_data["target_before_version"] == "v1.3.0"
        assert run_data["target_after_version"] == "v1.0.0"

        audit_trail = run_data["execution"]["audit_trail"]
        assert len(audit_trail) >= 4
        precheck_entries = [e for e in audit_trail if e["what"] == "prechecks_completed"]
        assert len(precheck_entries) == 1
        checks_list = precheck_entries[0]["evidence"]["checks"]
        assert [c["rule_id"] for c in checks_list] == [f"B{i}" for i in range(1, 11)]
        assert all(c["passed"] is True and isinstance(c["inputs"], dict) for c in checks_list)

        # GET /runs/{id}/events includes both run events and sanitized execution events
        events_resp = client.get(f"/runs/{s1_run_id}/events?limit=200")
        assert events_resp.status_code == 200, events_resp.text
        events_items = events_resp.json()["items"]
        sources = {item["source"] for item in events_items}
        assert sources == {"run", "execution"}
        event_types = [item["type"] for item in events_items]
        assert "prechecks_completed" in event_types
        assert "execution_acknowledged" in event_types

        print("\n--- OFFLINE S1 END-TO-END EVENT TIMELINE ---")
        for item in events_items:
            print(
                f"{item['created_at']} | {item['source']:9s} | {item['actor']:10s} | {item['type']}"
            )
        print("--- OFFLINE S1 AUDIT TRAIL (time, who, what, evidence_keys) ---")
        for entry in audit_trail:
            print(
                f"{entry['time']} | {entry['who']} | {entry['what']} | {sorted(entry['evidence'].keys())}"
            )

        # -------------------------------------------------------------------
        # S3: restart path (restart_service on lab-inventory)
        # -------------------------------------------------------------------
        _, s3_run_id, s3_plan_id, s3_hash = _seed_needs_review_run(
            session,
            tenant_id=tenant_id,
            case_id="S3",
            service="inventory",
            target_id="lab-inventory",
            action="restart_service",
            expected_version="v1.0.0",
            rollback_to_version=None,
        )
        s3_approve = client.post(
            f"/plans/{s3_plan_id}/decisions",
            headers={
                "Idempotency-Key": f"idem-s3-{uuid4().hex[:8]}",
                "If-Match": s3_hash,
                "X-CSRF-Token": "test-csrf",
            },
            cookies={SESSION_COOKIE_NAME: token, "opspilot_csrf": "test-csrf"},
            json={
                "decision": "approve",
                "submitted_plan_hash": s3_hash,
                "reason": "S3 dependency timeout restart approved",
            },
        )
        assert s3_approve.status_code == 200, s3_approve.text
        s3_exec_id = s3_approve.json()["action_execution_id"]
        _run_graph_through_await_execution(
            session,
            tenant_id=tenant_id,
            run_id=s3_run_id,
            plan_id=s3_plan_id,
            plan_hash_val=s3_hash,
            action_execution_id=s3_exec_id,
        )

        fake_s3 = FakeTarget({"lab-inventory": "v1.0.0"}, registry=registry)
        broker_s3 = BrokerService(
            session_factory=SessionLocal,
            target_client=fake_s3,
            target_registry=registry,
            broker_id="broker-offline-s3",
        )
        assert broker_s3.process_next() == "applied"
        assert fake_s3.call_count == 1
        assert fake_s3.versions["lab-inventory"] == "v1.0.0"

        s3_run_data = client.get(f"/runs/{s3_run_id}").json()
        assert s3_run_data["status"] == "verifying"
        assert s3_run_data["execution_status"] == "acknowledged"
        assert s3_run_data["target_before_version"] == "v1.0.0"
        assert s3_run_data["target_after_version"] == "v1.0.0"

        # -------------------------------------------------------------------
        # S5: false alarm -> completed with no plan and no execution
        # -------------------------------------------------------------------
        s5_inc_id = uuid4()
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        session.add(
            IncidentModel(
                id=s5_inc_id,
                tenant_id=tenant_id,
                alert_id=f"alert-s5-{uuid4().hex[:8]}",
                source="seeded_case",
                service="orders",
                severity="MEDIUM",
                alert_payload={"case_id": "S5"},
                opened_at=datetime.now(timezone.utc),
                status_summary="open",
            )
        )
        session.flush()
        ctx_w = TenantContext(tenant_id=tenant_id, role="worker")
        r_svc = RunService(session)
        s5_run = r_svc.create_run(ctx=ctx_w, incident_id=s5_inc_id, case_id="S5")
        s5_run, _ = r_svc.transition(
            ctx=ctx_w, run_id=s5_run.id, to_status="investigating", actor="worker", expected_version=s5_run.version
        )
        s5_run, _ = r_svc.transition(
            ctx=ctx_w,
            run_id=s5_run.id,
            to_status="completed",
            actor="worker",
            expected_version=s5_run.version,
            payload={"outcome_reason": "false_alarm_no_action"},
        )
        session.commit()

        s5_plans = session.scalars(
            select(ActionPlanModel).where(ActionPlanModel.run_id == s5_run.id)
        ).all()
        assert s5_plans == []
        s5_data = client.get(f"/runs/{s5_run.id}").json()
        assert s5_data["status"] == "completed"
        assert s5_data["proposed_plan_id"] is None
        assert s5_data["execution"] is None
        assert s5_data["execution_status"] is None

    # Verify db_invariants passes with 0 violations
    assert check_invariants() == 0
    engine.dispose()


@pytest.mark.lab
@contextmanager
def _override_db_session(session_factory: sessionmaker[Session]):
    def _override_db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.skipif(not _pg_available(), reason="Live PostgreSQL not reachable")
def test_live_e2e_orders_rollback_and_deploys_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 4 & 6: Live E2E against real lab orders from v1.3.0 -> v1.0.0 + lab.deploys."""
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    lab_db_url = os.environ.get(
        "LAB_DATABASE_URL",
        "postgresql://lab_admin:lab-postgres-local-pw@127.0.0.1:5432/lab",
    ).replace("+psycopg", "")
    monkeypatch.setenv("LAB_DATABASE_URL", lab_db_url)

    admin_token = SecretStr(os.environ.get("LAB_ADMIN_TOKEN", "super-secret-admin-token"))
    registry = load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    lab_client = LabAdminClient(registry=registry, admin_token=admin_token)

    # Ensure orders is at v1.3.0 before starting
    state = lab_client.get_state("lab-orders")
    if getattr(state, "current_version", None) != "v1.3.0":
        import httpx

        spec = registry.get_by_target_id("lab-orders")
        assert spec is not None
        resp = httpx.post(
            f"{spec.admin_base_url}/admin/deploy",
            headers={"X-Lab-Admin-Token": admin_token.get_secret_value()},
            json={"version": "v1.3.0", "actor": "e2e_setup"},
            timeout=5.0,
        )
        assert resp.status_code == 200, resp.text

    engine = create_engine(PG_URL, future=True)
    SessionLocal = sessionmaker(bind=engine, future=True)
    with psycopg.connect(PSYCOPG_URL) as raw_conn:
        _drain_active_executions(raw_conn)

    client = TestClient(app)
    with _override_db_session(SessionLocal), SessionLocal() as session:
        tenant_id, _, token = _setup_tenant_and_reviewer(session)
        client.cookies.set(SESSION_COOKIE_NAME, token)

        _, run_id, plan_id, p_hash = _seed_needs_review_run(
            session,
            tenant_id=tenant_id,
            case_id="S1",
            service="orders",
            target_id="lab-orders",
            action="rollback_service",
            expected_version="v1.3.0",
            rollback_to_version="v1.0.0",
        )

        approve_resp = client.post(
            f"/plans/{plan_id}/decisions",
            headers={
                "Idempotency-Key": f"idem-live-{uuid4().hex[:8]}",
                "If-Match": p_hash,
                "X-CSRF-Token": "test-csrf",
            },
            cookies={SESSION_COOKIE_NAME: token, "opspilot_csrf": "test-csrf"},
            json={
                "decision": "approve",
                "submitted_plan_hash": p_hash,
                "reason": "Live S1 rollback approved",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text
        exec_id = approve_resp.json()["action_execution_id"]

        _run_graph_through_await_execution(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            plan_id=plan_id,
            plan_hash_val=p_hash,
            action_execution_id=exec_id,
        )

        # Wait briefly if containerized broker is already running, else run BrokerService directly
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            session.expire_all()
            ex = session.get(ActionExecutionModel, UUID(exec_id))
            if ex is not None and ex.status == "acknowledged":
                break
            time.sleep(0.3)

        session.expire_all()
        ex = session.get(ActionExecutionModel, UUID(exec_id))
        if ex is None or ex.status != "acknowledged":
            broker = BrokerService(
                session_factory=SessionLocal,
                target_client=lab_client,
                target_registry=registry,
                broker_id="broker-live-e2e",
            )
            assert broker.process_next() == "applied"

        run_data = client.get(f"/runs/{run_id}").json()
        assert run_data["status"] == "verifying"
        assert run_data["execution_status"] == "acknowledged"
        assert run_data["target_before_version"] == "v1.3.0"
        assert run_data["target_after_version"] == "v1.0.0"

        events_items = client.get(f"/runs/{run_id}/events?limit=200").json()["items"]
        print("\n--- LIVE S1 END-TO-END EVENT TIMELINE ---")
        for item in events_items:
            print(
                f"{item['created_at']} | {item['source']:9s} | {item['actor']:10s} | {item['type']}"
            )

    # Verify new rollback row in lab.deploys
    with psycopg.connect(lab_db_url) as lab_conn:
        with lab_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, service, version, action, actor, deployed_at
                FROM lab.deploys
                WHERE service = 'orders'
                ORDER BY deployed_at DESC, id DESC
                LIMIT 1;
                """
            )
            deploy_row = cur.fetchone()
            assert deploy_row is not None
            assert deploy_row[1] == "orders"
            assert deploy_row[2] == "v1.0.0"
            print(f"\n--- LIVE lab.deploys ROW ---\n{deploy_row}")

    assert check_invariants(live=True) == 0
    engine.dispose()
