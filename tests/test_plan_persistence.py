"""Tests for PlanRepository persistence, atomicity, expiry, API routes, and SQL triggers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from apps.api.auth.dependencies import Principal, get_current_principal, get_db_session
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import ConcurrentModificationError
from apps.api.main import app
from apps.api.models.tables import (
    ActionPlanModel,
    IncidentModel,
    RunEventModel,
    RunModel,
    TenantModel,
)
from apps.api.repositories.plan_repository import PlanRepository
from shared.actions import ActionType
from shared.plans import ActionPlan, PlanStatus


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


def _sqlite_session_factory() -> sessionmaker[Session]:
    """Creates an in-memory SQLite engine with app schema and clock_timestamp() for unit tests."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _coerce_list_params(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> tuple[str, Any]:
        if isinstance(parameters, tuple):
            parameters = tuple(json.dumps(p) if isinstance(p, (list, dict)) else p for p in parameters)
        elif isinstance(parameters, list):
            parameters = [
                tuple(json.dumps(x) if isinstance(x, (list, dict)) else x for x in row)
                if isinstance(row, tuple)
                else (json.dumps(row) if isinstance(row, (list, dict)) else row)
                for row in parameters
            ]
        return statement, parameters

    @event.listens_for(engine, "connect")
    def _attach_app_schema(dbapi_conn: Any, _rec: Any) -> None:
        dbapi_conn.create_function(
            "clock_timestamp",
            0,
            lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
        )
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS app")
        dbapi_conn.executescript(
            """
            CREATE TABLE app.tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.incidents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                alert_id TEXT NOT NULL,
                source TEXT NOT NULL,
                service TEXT NOT NULL,
                severity TEXT NOT NULL,
                alert_payload TEXT NOT NULL,
                opened_at TIMESTAMP NOT NULL,
                status_summary TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                case_id TEXT,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                resolution TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                remaining_calls INTEGER NOT NULL DEFAULT 8,
                max_calls INTEGER NOT NULL DEFAULT 8,
                last_error TEXT,
                heartbeat_at TIMESTAMP,
                worker_id TEXT,
                created_by_user_id TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            );
            CREATE TABLE app.action_plans (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                service TEXT,
                target_id TEXT,
                expected_version TEXT,
                rollback_to_version TEXT,
                rationale_evidence_ids TEXT NOT NULL DEFAULT '[]',
                rollback_route TEXT,
                plan_hash TEXT NOT NULL,
                policy_version TEXT,
                status TEXT NOT NULL DEFAULT 'proposed',
                notes TEXT NOT NULL DEFAULT '',
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app.run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    return sessionmaker(bind=engine, expire_on_commit=False)


class _SessionRunService:
    """Minimal RunService using the same SQLAlchemy Session for atomic savepoint tests."""

    def __init__(self, session: Session, fail_transition: bool = False) -> None:
        self.session = session
        self.fail_transition = fail_transition
        self._seq = 0

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEventModel:
        self._seq += 1
        ev = RunEventModel(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            seq=self._seq,
            type=event_type,
            actor=actor,
            payload=json.dumps(payload or {}),  # type: ignore[arg-type]
        )
        self.session.add(ev)
        self.session.flush()
        return ev

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        resolution: str | None = None,
    ) -> tuple[RunModel, RunEventModel]:
        if self.fail_transition:
            raise ConcurrentModificationError(str(run_id), expected_version, expected_version + 1)
        run = self.session.scalar(
            select(RunModel).where(
                RunModel.id == run_id,
                RunModel.tenant_id == ctx.tenant_id,
            )
        )
        assert run is not None
        if run.version != expected_version:
            raise ConcurrentModificationError(str(run_id), expected_version, run.version)
        run.status = to_status
        run.version += 1
        self.session.flush()
        ev = self.append_event(ctx, run_id, "status_transition", actor, payload)
        return run, ev


class _NoRLSPRepo(PlanRepository):
    """PlanRepository subclass skipping PostgreSQL SET LOCAL for SQLite unit tests."""

    def set_tenant_context(self, ctx: TenantContext) -> None:  # type: ignore[override]
        return


def _seed_run(session: Session, tenant_id: UUID, run_id: UUID) -> None:
    inc_id = uuid4()
    session.add(TenantModel(id=tenant_id, name="Acme", slug=f"acme-{tenant_id.hex[:6]}"))
    session.add(
        IncidentModel(
            id=inc_id,
            tenant_id=tenant_id,
            alert_id="alt_1",
            source="alert",
            service="gateway",
            severity="CRITICAL",
            alert_payload="{}",  # type: ignore[arg-type]
            opened_at=datetime.now(timezone.utc),
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=inc_id,
            status="investigating",
            version=1,
        )
    )
    session.commit()


def _make_action_plan(tenant_id: UUID, run_id: UUID, plan_id: UUID | None = None) -> ActionPlan:
    now = datetime.now(timezone.utc)
    return ActionPlan(
        plan_id=str(plan_id or uuid4()),
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        target_id="lab-orders",
        expected_version="v1.3.0",
        rollback_to_version="v1.0.0",
        rollback_route="Re-deploy orders v1.3.0 (new proposal required)",
        rationale_evidence_ids=["ev_logs_01"],
        note="Rollback orders",
        status=PlanStatus.PROPOSED,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def test_create_and_supersede_atomicity_rolls_back_on_mid_tx_failure() -> None:
    factory = _sqlite_session_factory()
    tenant_id, run_id = uuid4(), uuid4()
    with factory() as session:
        _seed_run(session, tenant_id, run_id)
        ctx = TenantContext(tenant_id=tenant_id)
        repo = _NoRLSPRepo(session)

        p1_id = uuid4()
        p1 = _make_action_plan(tenant_id, run_id, p1_id)
        session.add(
            ActionPlanModel(
                id=p1_id,
                run_id=run_id,
                tenant_id=tenant_id,
                action=p1.action.value,
                service=p1.service,
                target_id=p1.target_id,
                expected_version=p1.expected_version,
                rollback_to_version=p1.rollback_to_version,
                rationale_evidence_ids='["ev_logs_01"]',  # type: ignore[arg-type]
                rollback_route=p1.rollback_route,
                plan_hash=p1.plan_hash,
                status="proposed",
                expires_at=p1.expires_at,
            )
        )
        session.commit()

        p2_id = uuid4()
        p2 = _make_action_plan(tenant_id, run_id, p2_id)
        failing_svc = _SessionRunService(session, fail_transition=True)
        with pytest.raises(ConcurrentModificationError):
            repo.create(ctx, p2, run_service=failing_svc, expected_run_version=1, policy_version="pol_v1")

        p1_row = session.scalar(select(ActionPlanModel).where(ActionPlanModel.id == p1_id))
        p2_row = session.scalar(select(ActionPlanModel).where(ActionPlanModel.id == p2_id))
        events = list(session.scalars(select(RunEventModel)).all())
        run_row = session.scalar(select(RunModel).where(RunModel.id == run_id))
        assert p1_row is not None and p1_row.status == "proposed"
        assert p2_row is None
        assert len(events) == 0
        assert run_row is not None and run_row.status == "investigating"


def test_concurrent_double_proposal_leaves_exactly_one_active_plan() -> None:
    factory = _sqlite_session_factory()
    tenant_id, run_id = uuid4(), uuid4()
    with factory() as session:
        _seed_run(session, tenant_id, run_id)
        ctx = TenantContext(tenant_id=tenant_id)
        repo = _NoRLSPRepo(session)
        svc = _SessionRunService(session)

        p1 = _make_action_plan(tenant_id, run_id)
        p2 = _make_action_plan(tenant_id, run_id)

        # Monkeypatch rationale_evidence_ids to JSON string for SQLite TEXT column binding
        p1_copy = p1
        repo.create(ctx, p1_copy, run_service=svc, expected_run_version=1, policy_version="pol_v1")
        session.commit()

        with pytest.raises(ConcurrentModificationError):
            repo.create(ctx, p2, run_service=svc, expected_run_version=1, policy_version="pol_v1")

        active_plans = list(
            session.scalars(
                select(ActionPlanModel).where(
                    ActionPlanModel.run_id == run_id,
                    ActionPlanModel.status == "proposed",
                )
            ).all()
        )
        assert len(active_plans) == 1
        assert str(active_plans[0].id) == p1.plan_id


def test_expire_overdue_plans_marks_expired_and_emits_events() -> None:
    factory = _sqlite_session_factory()
    tenant_id, run_id = uuid4(), uuid4()
    with factory() as session:
        _seed_run(session, tenant_id, run_id)
        ctx = TenantContext(tenant_id=tenant_id)
        repo = _NoRLSPRepo(session)
        svc = _SessionRunService(session)

        overdue_id, fresh_id = uuid4(), uuid4()
        now = datetime.now(timezone.utc)
        session.add(
            ActionPlanModel(
                id=overdue_id,
                run_id=run_id,
                tenant_id=tenant_id,
                action="rollback_service",
                service="orders",
                target_id="lab-orders",
                expected_version="v1.3.0",
                rollback_to_version="v1.0.0",
                rationale_evidence_ids='["ev_1"]',  # type: ignore[arg-type]
                rollback_route="Re-deploy orders v1.3.0",
                plan_hash="b" * 64,
                status="proposed",
                expires_at=now - timedelta(minutes=1),
            )
        )
        session.add(
            ActionPlanModel(
                id=fresh_id,
                run_id=run_id,
                tenant_id=tenant_id,
                action="rollback_service",
                service="orders",
                target_id="lab-orders",
                expected_version="v1.3.0",
                rollback_to_version="v1.0.0",
                rationale_evidence_ids='["ev_1"]',  # type: ignore[arg-type]
                rollback_route="Re-deploy orders v1.3.0",
                plan_hash="c" * 64,
                status="proposed",
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.commit()

        expired = repo.expire_overdue_plans(ctx, run_service=svc)
        assert expired == [overdue_id]
        overdue_row = session.scalar(select(ActionPlanModel).where(ActionPlanModel.id == overdue_id))
        fresh_row = session.scalar(select(ActionPlanModel).where(ActionPlanModel.id == fresh_id))
        assert overdue_row is not None and overdue_row.status == "expired"
        assert fresh_row is not None and fresh_row.status == "proposed"
        events = list(session.scalars(select(RunEventModel)).all())
        assert len(events) == 1
        assert events[0].type == "plan_expired"


def test_api_get_plan_roles_tenant_isolation_and_no_mutation_routes() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    plan_id, run_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)

    class _FakePlanObj:
        def __init__(self) -> None:
            self.id = plan_id
            self.run_id = run_id
            self.tenant_id = tenant_a
            self.action = "rollback_service"
            self.service = "orders"
            self.target_id = "lab-orders"
            self.expected_version = "v1.3.0"
            self.rollback_to_version = "v1.0.0"
            self.rationale_evidence_ids = ["ev_logs_01"]
            self.rollback_route = "Re-deploy orders v1.3.0 (new proposal required)"
            self.plan_hash = "a" * 64
            self.policy_version = "pol_v1"
            self.status = "proposed"
            self.notes = "test"
            self.expires_at = now + timedelta(minutes=15)
            self.created_at = now
            self.updated_at = now

    class _MockSession:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            return None

        def scalar(self, *args: Any, **kwargs: Any) -> Any:
            if args and hasattr(args[0], "compile"):
                params = args[0].compile().params
                if tenant_a in params.values() or str(tenant_a) in params.values():
                    return _FakePlanObj()
            return None

    def _override_db() -> Any:
        yield _MockSession()

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid4(), tenant_id=tenant_a, role="viewer"
    )
    try:
        client = TestClient(app)
        resp = client.get(f"/plans/{plan_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["target_id"] == "lab-orders"
        assert data["rollback_to_version"] == "v1.0.0"
        assert data["policy_version"] == "pol_v1"
        assert data["plan_hash"] == "a" * 64

        assert client.put(f"/plans/{plan_id}", json={}).status_code == 405
        assert client.patch(f"/plans/{plan_id}", json={}).status_code == 405
        assert client.delete(f"/plans/{plan_id}").status_code == 405

        app.dependency_overrides[get_current_principal] = lambda: Principal(
            user_id=uuid4(), tenant_id=tenant_a, role="reviewer"
        )
        assert client.get(f"/plans/{plan_id}").status_code == 200
        # Without CSRF header, POST /decisions returns 403; with CSRF header but no If-Match, 428
        dec_no_csrf = client.post(
            f"/plans/{plan_id}/decisions",
            json={"decision": "approve", "submitted_plan_hash": "a" * 64, "reason": "ok"},
        )
        assert dec_no_csrf.status_code == 403
        dec_no_if_match = client.post(
            f"/plans/{plan_id}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "Idempotency-Key": "k1"},
            json={"decision": "approve", "submitted_plan_hash": "a" * 64, "reason": "ok"},
        )
        assert dec_no_if_match.status_code == 428

        app.dependency_overrides[get_current_principal] = lambda: Principal(
            user_id=uuid4(), tenant_id=tenant_b, role="reviewer"
        )
        assert client.get(f"/plans/{plan_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_db_immutability_trigger_rejects_direct_sql_update() -> None:
    """Runs against live Postgres when TEST_DB_URL / 127.0.0.1:54333 is reachable."""
    db_url = os.environ.get("TEST_DB_URL", "postgresql://opspilot@127.0.0.1:54333/opspilot")
    try:
        conn = psycopg.connect(db_url, connect_timeout=1, autocommit=True)
    except psycopg.Error:
        pytest.skip("PostgreSQL test DB not reachable on 127.0.0.1:54333")
        return

    try:
        with conn.cursor() as cur:
            t_id, inc_id, run_id, plan_id = uuid4(), uuid4(), uuid4(), uuid4()
            cur.execute("INSERT INTO app.tenants (id, name, slug) VALUES (%s, 'T', %s)", (t_id, f"t-{t_id.hex[:6]}"))
            cur.execute("SET ROLE opspilot_api")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(t_id),))
            cur.execute(
                "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at) "
                "VALUES (%s, %s, 'a1', 'alert', 'gateway', 'CRITICAL', '{}'::jsonb, now())",
                (inc_id, t_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status, version) VALUES (%s, %s, %s, 'investigating', 1)",
                (run_id, t_id, inc_id),
            )
            plan = _make_action_plan(t_id, run_id, plan_id)
            cur.execute(
                "INSERT INTO app.action_plans (id, run_id, tenant_id, action, service, target_id, expected_version, rollback_to_version, rationale_evidence_ids, rollback_route, plan_hash, status, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed', %s)",
                (
                    plan_id,
                    run_id,
                    t_id,
                    plan.action.value,
                    plan.service,
                    plan.target_id,
                    plan.expected_version,
                    plan.rollback_to_version,
                    plan.rationale_evidence_ids,
                    plan.rollback_route,
                    plan.plan_hash,
                    plan.expires_at,
                ),
            )
            with pytest.raises(psycopg.Error) as exc:
                cur.execute("UPDATE app.action_plans SET plan_hash = %s WHERE id = %s", ("0" * 64, plan_id))
            assert "Immutable plan fields" in str(exc.value)
    finally:
        conn.close()
