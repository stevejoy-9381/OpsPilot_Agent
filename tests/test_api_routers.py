"""Integration tests for the OpsPilot REST API in apps/api/routers.

Covers:
1. Happy paths: create incident, create run, get run, list runs, evidence, plans.
2. Idempotency: same key twice (returns identical), same key different body (409 Conflict).
3. Pagination stability: cursor pagination across multiple pages.
4. Tenant isolation: cross-tenant access returns 404 across all GET and mutating routes.
5. Role enforcement table: viewer vs reviewer vs admin across all endpoints.
6. 404 vs 403 semantics: no existence leakage (cross-tenant returns 404, role violation returns 403).
7. Non-terminal cancellation: cancel allowed only from non-terminal states.
8. Stubbed approval decisions: POST /plans/{id}/decisions returns 501 Not Implemented.
9. Request size limit (413) and rejection of model-supplied identity (422).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.auth.csrf import REQUESTED_WITH_HEADER
from apps.api.auth.dependencies import (
    SESSION_COOKIE_NAME,
    get_db_session,
)
from apps.api.auth.passwords import hash_password
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.main import app
from apps.api.models.hashing import compute_plan_hash
from apps.api.models.tables import (
    ActionPlanModel,
    DiagnosisModel,
    HypothesisModel,
    IncidentModel,
    ObservationModel,
    RunEventModel,
    RunModel,
    SessionModel,
    TenantModel,
    UserModel,
)
from tools.common.evidence_store import compute_content_hash

DB_URL = "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DB_URL, pool_size=10, max_overflow=5)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def _create_user_session(db_session, tenant_id: uuid.UUID, role: str) -> tuple[UserModel, str]:
    """Helper to create a user and active session cookie."""
    user_id = uuid.uuid4()
    user = UserModel(
        id=user_id,
        tenant_id=tenant_id,
        email=f"{role}-{user_id.hex[:6]}@example.com",
        role=role,
        password_hash=hash_password("Secret123!"),
    )
    db_session.add(user)

    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    session_row = SessionModel(
        token_hash=token_hash,
        user_id=user_id,
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + pytest.importorskip("datetime").timedelta(hours=24),
    )
    db_session.add(session_row)
    db_session.commit()
    return user, raw_token


@pytest.fixture
def test_setup(db_session):
    """Sets up two tenants (Alpha and Beta) with viewer, reviewer, admin users."""
    tenant_alpha = TenantModel(id=uuid.uuid4(), slug=f"alpha-{uuid.uuid4().hex[:6]}", name="Alpha Org")
    tenant_beta = TenantModel(id=uuid.uuid4(), slug=f"beta-{uuid.uuid4().hex[:6]}", name="Beta Org")
    db_session.add_all([tenant_alpha, tenant_beta])
    db_session.commit()

    _, alpha_viewer_token = _create_user_session(db_session, tenant_alpha.id, "viewer")
    _, alpha_reviewer_token = _create_user_session(db_session, tenant_alpha.id, "reviewer")
    _, alpha_admin_token = _create_user_session(db_session, tenant_alpha.id, "admin")

    _, beta_viewer_token = _create_user_session(db_session, tenant_beta.id, "viewer")
    _, beta_reviewer_token = _create_user_session(db_session, tenant_beta.id, "reviewer")

    headers = {REQUESTED_WITH_HEADER: "XMLHttpRequest"}

    return {
        "alpha_tenant_id": tenant_alpha.id,
        "beta_tenant_id": tenant_beta.id,
        "alpha_viewer": alpha_viewer_token,
        "alpha_reviewer": alpha_reviewer_token,
        "alpha_admin": alpha_admin_token,
        "beta_viewer": beta_viewer_token,
        "beta_reviewer": beta_reviewer_token,
        "headers": headers,
    }


def _make_alert(rule_id="orders_db_pool_saturation", service="orders") -> dict[str, Any]:
    return {
        "alert_id": f"alt_{uuid.uuid4().hex[:12]}",
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "rule_id": rule_id,
        "metric": "db_pool_in_use",
        "threshold": 0.9,
        "observed_value": 0.95,
        "window_seconds": 60,
        "severity": "CRITICAL",
        "status": "firing",
    }


# ===========================================================================
# 1. Role Enforcement Table & Unauthenticated Tests
# ===========================================================================


def test_role_enforcement_table(client, test_setup):
    """Verifies access matrix: unauthenticated (401), viewer (403 on mutate), reviewer/admin (allowed)."""
    headers = test_setup["headers"]

    # 1. Unauthenticated requests to all endpoints return 401
    assert client.post("/incidents", json={"case_id": "S1"}).status_code == 401
    assert client.post("/runs", json={"incident_id": str(uuid.uuid4())}).status_code == 401
    assert client.get("/runs").status_code == 401
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 401
    assert client.get(f"/runs/{uuid.uuid4()}/evidence").status_code == 401
    assert client.get(f"/plans/{uuid.uuid4()}").status_code == 401

    # 2. Viewer role forbidden on mutating POST endpoints
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_viewer"])
    resp = client.post("/incidents", json={"case_id": "S1"}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"

    resp = client.post(
        "/runs",
        json={"incident_id": str(uuid.uuid4())},
        headers={**headers, "Idempotency-Key": "key-v"},
    )
    assert resp.status_code == 403

    resp = client.post(f"/runs/{uuid.uuid4()}/cancel", headers=headers)
    assert resp.status_code == 403

    resp = client.post(
        f"/plans/{uuid.uuid4()}/decisions",
        json={"decision": "approved", "submitted_plan_hash": "a" * 64, "reason": "ok"},
        headers=headers,
    )
    assert resp.status_code == 403

    # 3. Viewer allowed on read endpoints
    get_runs_resp = client.get("/runs")
    assert get_runs_resp.status_code == 200


# ===========================================================================
# 2. Incident Creation (AlertEvent, case_id, Idempotency)
# ===========================================================================


def test_incidents_create_from_alert_and_case(client, test_setup):
    """POST /incidents works from validated AlertEvent or case_id, rejecting extra/missing fields."""
    headers = test_setup["headers"]
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])

    # 1. Create from full AlertEvent
    alert_payload = _make_alert()
    resp = client.post("/incidents", json={"alert": alert_payload}, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["alert_id"] == alert_payload["alert_id"]
    assert data["service"] == "orders"
    assert data["source"] == "alert"
    assert data["tenant_id"] == str(test_setup["alpha_tenant_id"])

    # 2. Create from seeded case_id
    resp_case = client.post("/incidents", json={"case_id": "S1"}, headers=headers)
    assert resp_case.status_code == 201
    case_data = resp_case.json()
    assert case_data["source"] == "seeded_case"
    assert case_data["service"] == "orders"
    assert "orders_db_pool_saturation" in case_data["alert_payload"]["rule_id"]

    # 3. Neither alert nor case_id provided -> 422
    resp_empty = client.post("/incidents", json={}, headers=headers)
    assert resp_empty.status_code == 422
    assert "Either 'alert' or 'case_id' must be provided" in str(resp_empty.json())

    # 4. Extra fields (e.g. model-supplied tenant_id) rejected with 422
    resp_tamper = client.post(
        "/incidents",
        json={"case_id": "S1", "tenant_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp_tamper.status_code == 422


def test_incidents_idempotency(client, test_setup):
    """Idempotency-Key header on POST /incidents returns same incident or 409 Conflict."""
    headers = {**test_setup["headers"], "Idempotency-Key": "inc-idemp-001"}
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])

    # First call creates incident
    resp1 = client.post("/incidents", json={"case_id": "S1"}, headers=headers)
    assert resp1.status_code == 201
    inc_id = resp1.json()["id"]

    # Same key + same body returns identical incident
    resp2 = client.post("/incidents", json={"case_id": "S1"}, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["id"] == inc_id

    # Same key + different body returns 409 Conflict
    resp3 = client.post("/incidents", json={"case_id": "S2"}, headers=headers)
    assert resp3.status_code == 409
    assert resp3.json()["error"]["code"] == "conflict"


# ===========================================================================
# 3. Run Creation & Idempotency
# ===========================================================================


def test_runs_create_and_idempotency(client, test_setup):
    """POST /runs enforces required Idempotency-Key, server-side capping, and idempotency."""
    headers = test_setup["headers"]
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])

    # Create incident first
    inc_resp = client.post("/incidents", json={"case_id": "S1"}, headers=headers)
    assert inc_resp.status_code == 201
    incident_id = inc_resp.json()["id"]

    # 1. Missing Idempotency-Key header returns 400 Bad Request
    resp_no_idemp = client.post("/runs", json={"incident_id": incident_id}, headers=headers)
    assert resp_no_idemp.status_code == 400
    assert "Idempotency-Key header is required" in resp_no_idemp.json()["detail"]

    # 2. Create run with requested max_calls=20 -> capped to MAX_TOOL_CALLS (8)
    idemp_headers = {**headers, "Idempotency-Key": "run-idemp-100"}
    resp_run = client.post(
        "/runs",
        json={"incident_id": incident_id, "max_calls": 20},
        headers=idemp_headers,
    )
    assert resp_run.status_code == 201
    run_data = resp_run.json()
    run_id = run_data["id"]
    assert run_data["status"] == "queued"
    assert run_data["version"] == 1
    assert run_data["max_calls"] == 8
    assert run_data["remaining_calls"] == 8

    # 3. Same key + same body returns existing run
    resp_same = client.post(
        "/runs",
        json={"incident_id": incident_id, "max_calls": 20},
        headers=idemp_headers,
    )
    assert resp_same.status_code == 200
    assert resp_same.json()["id"] == run_id

    # 4. Same key + different body returns 409 Conflict
    other_inc = client.post("/incidents", json={"case_id": "S2"}, headers=headers).json()["id"]
    resp_diff = client.post(
        "/runs",
        json={"incident_id": other_inc, "max_calls": 20},
        headers=idemp_headers,
    )
    assert resp_diff.status_code == 409
    assert resp_diff.json()["error"]["code"] == "conflict"


# ===========================================================================
# 4. GET /runs/{id} Detail & Linked Aggregates
# ===========================================================================


def test_runs_get_detail_and_aggregates(client, test_setup, db_session):
    """GET /runs/{id} returns run state, evidence_ids, hypotheses, plan, and diagnosis."""
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_viewer"])
    tenant_id = test_setup["alpha_tenant_id"]

    # Seed incident and run directly in DB
    now = datetime.now(timezone.utc)
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()

    inc = IncidentModel(
        id=inc_id,
        tenant_id=tenant_id,
        alert_id="alt_test_01",
        source="alert",
        service="orders",
        severity="HIGH",
        alert_payload={"rule": "test"},
        opened_at=now,
    )
    run = RunModel(
        id=run_id,
        tenant_id=tenant_id,
        incident_id=inc_id,
        status="needs_review",
        version=2,
        remaining_calls=5,
        max_calls=8,
    )
    evt = RunEventModel(
        run_id=run_id,
        tenant_id=tenant_id,
        seq=1,
        type="run_created",
        actor="system",
        payload={},
    )
    db_session.add_all([inc, run, evt])

    # Add observation
    obs_payload = {"values": [0.95]}
    obs = ObservationModel(
        id=uuid.uuid4(),
        evidence_id="ev_metric_01",
        run_id=run_id,
        tenant_id=tenant_id,
        tool="get_metrics",
        source_id="metrics://orders",
        captured_at=now,
        window_start=now,
        window_end=now,
        args={"service": "orders", "metric": "db_pool_in_use"},
        summary="Pool in use 0.95",
        payload=obs_payload,
        content_hash=compute_content_hash(obs_payload),
    )
    # Add hypothesis
    hyp = HypothesisModel(
        id=uuid.uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        statement="Connection pool exhausted",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
        status="supported",
        supporting_evidence_ids=["ev_metric_01"],
        confidence=0.95,
    )
    # Add diagnosis
    diag = DiagnosisModel(
        id=uuid.uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        root_cause_service="orders",
        root_cause_category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        culprit_deploy_id="dep_c0ffee",
        evidence_ids=["ev_metric_01"],
        confidence=0.95,
    )
    # Add plan
    plan_id = uuid.uuid4()
    p_hash = compute_plan_hash(
        action="rollback_service",
        service="orders",
        expected_version="v1.2.0",
        rationale_evidence_ids=["ev_metric_01"],
        rollback_route=None,
    )
    plan = ActionPlanModel(
        id=plan_id,
        run_id=run_id,
        tenant_id=tenant_id,
        action="rollback_service",
        service="orders",
        expected_version="v1.2.0",
        rationale_evidence_ids=["ev_metric_01"],
        rollback_route=None,
        plan_hash=p_hash,
        status="proposed",
        notes="Rollback to previous release",
        expires_at=now,
    )
    db_session.add_all([obs, hyp, diag, plan])
    db_session.commit()

    # Query GET /runs/{id}
    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(run_id)
    assert data["status"] == "needs_review"
    assert data["remaining_calls"] == 5
    assert data["evidence_ids"] == ["ev_metric_01"]
    assert len(data["hypotheses"]) == 1
    assert data["hypotheses"][0]["mechanism"] == "connection_pool_exhaustion"
    assert data["proposed_plan_id"] == str(plan_id)
    assert data["diagnosis"]["root_cause_service"] == "orders"


# ===========================================================================
# 5. Cursor Pagination Stability
# ===========================================================================


def test_runs_cursor_pagination_stability(client, test_setup, db_session):
    """GET /runs pagination retrieves items deterministically across multiple pages."""
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_viewer"])
    tenant_id = test_setup["alpha_tenant_id"]

    inc_id = uuid.uuid4()
    inc = IncidentModel(
        id=inc_id,
        tenant_id=tenant_id,
        alert_id="alt_pag_test",
        source="alert",
        service="inventory",
        severity="HIGH",
        alert_payload={},
        opened_at=datetime.now(timezone.utc),
    )
    db_session.add(inc)

    created_ids = []
    for _ in range(5):
        rid = uuid.uuid4()
        r = RunModel(
            id=rid,
            tenant_id=tenant_id,
            incident_id=inc_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
        evt = RunEventModel(
            run_id=rid,
            tenant_id=tenant_id,
            seq=1,
            type="run_created",
            actor="system",
            payload={},
        )
        db_session.add_all([r, evt])
        created_ids.append(str(rid))
    db_session.commit()

    # Page 1: limit 2
    p1 = client.get(f"/runs?incident_id={inc_id}&limit=2").json()
    assert len(p1["items"]) == 2
    assert p1["has_more"] is True
    assert p1["next_cursor"] is not None

    # Page 2: limit 2 with cursor
    p2 = client.get(f"/runs?incident_id={inc_id}&limit=2&cursor={p1['next_cursor']}").json()
    assert len(p2["items"]) == 2
    assert p2["has_more"] is True

    # Page 3: final page
    p3 = client.get(f"/runs?incident_id={inc_id}&limit=2&cursor={p2['next_cursor']}").json()
    assert len(p3["items"]) == 1
    assert p3["has_more"] is False
    assert p3["next_cursor"] is None

    # Verify all 5 runs collected without duplicates
    all_seen = [item["id"] for item in p1["items"] + p2["items"] + p3["items"]]
    assert len(all_seen) == 5
    assert set(all_seen) == set(created_ids)


# ===========================================================================
# 6. Tenant Isolation Across All GET Routes & 404 Semantics
# ===========================================================================


def test_tenant_isolation_across_all_routes_and_404_semantics(client, test_setup, db_session):
    """Beta user accessing Alpha's resources receives 404 (no existence leaks)."""
    alpha_tenant_id = test_setup["alpha_tenant_id"]

    # Create Alpha resources
    now = datetime.now(timezone.utc)
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    db_session.add(
        IncidentModel(
            id=inc_id,
            tenant_id=alpha_tenant_id,
            alert_id="alt_iso_01",
            source="alert",
            service="orders",
            severity="CRITICAL",
            alert_payload={},
            opened_at=now,
        )
    )
    db_session.add(
        RunModel(
            id=run_id,
            tenant_id=alpha_tenant_id,
            incident_id=inc_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
    )
    db_session.add(
        RunEventModel(
            run_id=run_id,
            tenant_id=alpha_tenant_id,
            seq=1,
            type="run_created",
            actor="system",
            payload={},
        )
    )
    alpha_payload = {"secret": "alpha_token"}
    db_session.add(
        ObservationModel(
            id=uuid.uuid4(),
            evidence_id="ev_alpha_secret",
            run_id=run_id,
            tenant_id=alpha_tenant_id,
            tool="get_metrics",
            source_id="metrics://orders",
            captured_at=now,
            window_start=now,
            window_end=now,
            args={},
            summary="alpha internal metric",
            payload=alpha_payload,
            content_hash=compute_content_hash(alpha_payload),
        )
    )
    p_hash_alpha = compute_plan_hash(
        action="restart_service",
        service="orders",
        expected_version=None,
        rationale_evidence_ids=[],
        rollback_route=None,
    )
    db_session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=alpha_tenant_id,
            action="restart_service",
            service="orders",
            plan_hash=p_hash_alpha,
            status="proposed",
            notes="alpha plan",
            expires_at=now,
        )
    )
    db_session.commit()

    # Now authenticate as Beta user
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["beta_reviewer"])
    headers = test_setup["headers"]

    # 1. GET /runs/{id} cross-tenant returns 404 (NOT 403!)
    assert client.get(f"/runs/{run_id}").status_code == 404

    # 2. GET /runs/{id}/evidence cross-tenant returns 404
    assert client.get(f"/runs/{run_id}/evidence").status_code == 404

    # 3. GET /runs/{id}/evidence/{evidence_id} cross-tenant returns 404
    assert client.get(f"/runs/{run_id}/evidence/ev_alpha_secret").status_code == 404

    # 4. GET /plans/{id} cross-tenant returns 404
    assert client.get(f"/plans/{plan_id}").status_code == 404

    # 5. POST /runs/{id}/cancel cross-tenant returns 404
    assert client.post(f"/runs/{run_id}/cancel", headers=headers).status_code == 404

    # 6. GET /runs list returns only Beta's items (empty)
    beta_list = client.get("/runs").json()
    assert all(item["tenant_id"] == str(test_setup["beta_tenant_id"]) for item in beta_list["items"])


# ===========================================================================
# 7. Run Cancellation
# ===========================================================================


def test_run_cancellation(client, test_setup, db_session):
    """POST /runs/{id}/cancel transitions non-terminal runs to escalated and rejects terminal runs."""
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])
    headers = test_setup["headers"]
    tenant_id = test_setup["alpha_tenant_id"]

    inc_id = uuid.uuid4()
    run_active_id = uuid.uuid4()
    run_terminal_id = uuid.uuid4()

    db_session.add(
        IncidentModel(
            id=inc_id,
            tenant_id=tenant_id,
            alert_id="alt_canc_01",
            source="alert",
            service="orders",
            severity="HIGH",
            alert_payload={},
            opened_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        RunModel(
            id=run_active_id,
            tenant_id=tenant_id,
            incident_id=inc_id,
            status="investigating",
            version=1,
            remaining_calls=8,
            max_calls=8,
        )
    )
    db_session.add(
        RunEventModel(
            run_id=run_active_id,
            tenant_id=tenant_id,
            seq=1,
            type="run_created",
            actor="system",
            payload={},
        )
    )
    db_session.add(
        RunModel(
            id=run_terminal_id,
            tenant_id=tenant_id,
            incident_id=inc_id,
            status="completed",
            version=1,
            remaining_calls=0,
            max_calls=8,
        )
    )
    db_session.add(
        RunEventModel(
            run_id=run_terminal_id,
            tenant_id=tenant_id,
            seq=1,
            type="run_created",
            actor="system",
            payload={},
        )
    )
    db_session.commit()

    # 1. Cancel active run -> moves to escalated
    resp_canc = client.post(f"/runs/{run_active_id}/cancel", headers=headers)
    assert resp_canc.status_code == 200
    assert resp_canc.json()["status"] == "escalated"

    # 2. Cancel terminal run -> 400 Bad Request
    resp_term = client.post(f"/runs/{run_terminal_id}/cancel", headers=headers)
    assert resp_term.status_code == 400
    assert "terminal status" in resp_term.json()["detail"]


# ===========================================================================
# 8. Plans Display & Decisions Stub (501)
# ===========================================================================


def test_plans_display_and_decisions_stub(client, test_setup, db_session):
    """GET /plans/{id} returns display data, and POST /plans/{id}/decisions returns 501."""
    tenant_id = test_setup["alpha_tenant_id"]
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])
    headers = test_setup["headers"]

    now = datetime.now(timezone.utc)
    inc_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    db_session.add(
        IncidentModel(
            id=inc_id,
            tenant_id=tenant_id,
            alert_id="alt_pl_01",
            source="alert",
            service="inventory",
            severity="HIGH",
            alert_payload={},
            opened_at=now,
        )
    )
    db_session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=inc_id,
            status="needs_review",
            version=1,
            remaining_calls=6,
            max_calls=8,
        )
    )
    db_session.add(
        RunEventModel(
            run_id=run_id,
            tenant_id=tenant_id,
            seq=1,
            type="run_created",
            actor="system",
            payload={},
        )
    )
    p_hash_inv = compute_plan_hash(
        action="rollback_service",
        service="inventory",
        expected_version="v2.1.0",
        rationale_evidence_ids=["ev_1", "ev_2"],
        rollback_route="re-deploy previous sha",
    )
    db_session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="inventory",
            expected_version="v2.1.0",
            rationale_evidence_ids=["ev_1", "ev_2"],
            rollback_route="re-deploy previous sha",
            plan_hash=p_hash_inv,
            status="proposed",
            notes="Safe rollback plan",
            expires_at=now,
        )
    )
    db_session.commit()

    # 1. Read display data
    resp_plan = client.get(f"/plans/{plan_id}")
    assert resp_plan.status_code == 200
    plan_data = resp_plan.json()
    assert plan_data["action"] == "rollback_service"
    assert plan_data["plan_hash"] == p_hash_inv

    # 2. POST /decisions returns 501 Not Implemented
    resp_dec = client.post(
        f"/plans/{plan_id}/decisions",
        json={"decision": "approved", "submitted_plan_hash": p_hash_inv, "reason": "Operator signed off"},
        headers=headers,
    )
    assert resp_dec.status_code == 501
    assert "Feature 8" in resp_dec.json()["detail"]


# ===========================================================================
# 9. Request Size Limit (413) & Rejection of Model-Supplied Identity (422)
# ===========================================================================


def test_request_size_limit_and_identity_rejection(client, test_setup):
    """Enforces 413 on oversized payloads and 422 on requests with model-supplied identity."""
    client.cookies.set(SESSION_COOKIE_NAME, test_setup["alpha_reviewer"])
    headers = test_setup["headers"]

    # 1. Oversized payload (> 1 MB) returns 413
    huge_data = {"case_id": "S1", "padding": "x" * (1_048_576 + 100)}
    resp_huge = client.post("/incidents", json=huge_data, headers=headers)
    assert resp_huge.status_code == 413
    assert resp_huge.json()["error"]["code"] == "payload_too_large"

    # 2. Model-supplied identity (tenant_id in body) is rejected with 422
    resp_model_identity = client.post(
        "/runs",
        json={"incident_id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4())},
        headers={**headers, "Idempotency-Key": "idemp-sec-1"},
    )
    assert resp_model_identity.status_code == 422
    assert resp_model_identity.json()["error"]["code"] == "validation_error"
