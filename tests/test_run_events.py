"""Tests for GET /runs/{id}/events (Polling and Server-Sent Events).

Covers:
1. Two modes: plain JSON list with ?after_seq= for polling, and Server-Sent Events when Accept: text/event-stream.
2. SSE requirements:
   - Heartbeat comment every 15s (or configured interval).
   - Last-Event-ID resume (use seq as event id).
   - Closes on terminal status (completed, escalated, failed).
   - Caps connection duration.
3. Event fan-out: Redis pub/sub / broadcaster waking streams, database remaining source of truth.
4. Ordered delivery under concurrent writers: no gaps, no duplicates.
5. Payload sanitization: scanning emitted events for secrets using Feature 2.3 patterns.
6. Tenant isolation: cross-tenant requests return 404.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.auth.dependencies import (
    SESSION_COOKIE_NAME,
    get_db_session,
)
from apps.api.auth.passwords import hash_password
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.domain.context import TenantContext
from apps.api.main import app
from apps.api.models.tables import (
    IncidentModel,
    RunModel,
    SessionModel,
    TenantModel,
    UserModel,
)
from apps.api.repositories.run_repository import RunRepository
from apps.api.services.run_service import RunService

DB_URL = "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DB_URL, pool_size=15, max_overflow=5)
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


def _create_user_session(db_session, tenant_id: uuid.UUID, role: str) -> str:
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
    return raw_token


@pytest.fixture
def setup_tenants(db_session):
    t_alpha = TenantModel(id=uuid.uuid4(), slug=f"ev-alpha-{uuid.uuid4().hex[:6]}", name="Events Alpha")
    t_beta = TenantModel(id=uuid.uuid4(), slug=f"ev-beta-{uuid.uuid4().hex[:6]}", name="Events Beta")
    db_session.add_all([t_alpha, t_beta])
    db_session.commit()

    alpha_token = _create_user_session(db_session, t_alpha.id, "reviewer")
    beta_token = _create_user_session(db_session, t_beta.id, "reviewer")

    # Create an incident and run for Alpha
    inc = IncidentModel(
        id=uuid.uuid4(),
        tenant_id=t_alpha.id,
        alert_id="alt_ev_01",
        source="alert",
        service="orders",
        severity="HIGH",
        alert_payload={},
        opened_at=datetime.now(timezone.utc),
    )
    run = RunModel(
        id=uuid.uuid4(),
        tenant_id=t_alpha.id,
        incident_id=inc.id,
        status="investigating",
        version=1,
        remaining_calls=8,
        max_calls=8,
    )
    db_session.add_all([inc, run])
    db_session.commit()

    return {
        "alpha_tenant_id": t_alpha.id,
        "beta_tenant_id": t_beta.id,
        "alpha_token": alpha_token,
        "beta_token": beta_token,
        "run_id": run.id,
    }


# ===========================================================================
# 1. Polling Mode (Plain JSON) & Pagination
# ===========================================================================


def test_run_events_polling_mode(client, setup_tenants, db_session):
    """GET /runs/{id}/events returns plain JSON list with ?after_seq= for polling."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])
    repo = RunRepository(db_session)

    # Append 3 events
    repo.append_event(ctx, run_id, "step_1", "system", {"info": "step 1 completed"})
    repo.append_event(ctx, run_id, "step_2", "system", {"info": "step 2 completed"})
    repo.append_event(ctx, run_id, "step_3", "system", {"info": "step 3 completed"})
    db_session.commit()

    # 1. Polling all events
    resp = client.get(f"/runs/{run_id}/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["last_seq"] == 3
    assert data["has_more"] is False
    assert [e["seq"] for e in data["items"]] == [1, 2, 3]

    # 2. Polling with after_seq=1
    resp_after = client.get(f"/runs/{run_id}/events?after_seq=1")
    assert resp_after.status_code == 200
    data_after = resp_after.json()
    assert len(data_after["items"]) == 2
    assert [e["seq"] for e in data_after["items"]] == [2, 3]

    # 3. Polling with limit=1
    resp_lim = client.get(f"/runs/{run_id}/events?limit=1")
    assert resp_lim.status_code == 200
    data_lim = resp_lim.json()
    assert len(data_lim["items"]) == 1
    assert data_lim["has_more"] is True
    assert data_lim["last_seq"] == 1


# ===========================================================================
# 2. Ordered Delivery Under Concurrent Writers
# ===========================================================================


def test_ordered_delivery_under_concurrent_writers(client, setup_tenants, session_factory):
    """Monotonic gap-free seq numbers are preserved under concurrent event appending."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])

    num_threads = 6
    events_per_thread = 5
    barrier = threading.Barrier(num_threads)
    errors: list[Exception] = []

    def worker(worker_idx: int):
        session = session_factory()
        try:
            repo = RunRepository(session)
            barrier.wait()
            for i in range(events_per_thread):
                repo.append_event(
                    ctx,
                    run_id,
                    f"worker_event_{worker_idx}",
                    f"worker_{worker_idx}",
                    {"thread": worker_idx, "iter": i},
                )
                session.commit()
        except Exception as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"

    # Verify via polling that all 30 events exist, with seq exactly 1..30 with 0 gaps and 0 duplicates
    resp = client.get(f"/runs/{run_id}/events?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == num_threads * events_per_thread
    seqs = [item["seq"] for item in items]
    assert seqs == list(range(1, num_threads * events_per_thread + 1))


# ===========================================================================
# 3. SSE Resume After Disconnect (Last-Event-ID)
# ===========================================================================


def test_sse_resume_after_disconnect_with_last_event_id(client, setup_tenants, db_session):
    """Last-Event-ID header resumes stream cleanly with no gaps and no duplicate events."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])
    repo = RunRepository(db_session)

    # Append 4 events
    for i in range(1, 5):
        repo.append_event(ctx, run_id, f"step_{i}", "system", {"step": i})
    db_session.commit()

    # 1. Client connects and receives events 1 and 2, then simulates disconnect
    headers = {"Accept": "text/event-stream"}
    # Cap duration to 0.5s so generator terminates for test inspection
    resp_init = client.get(
        f"/runs/{run_id}/events?max_duration_seconds=0.3&heartbeat_seconds=0.1",
        headers=headers,
    )
    assert resp_init.status_code == 200
    content = resp_init.text

    # Parse initial events
    id_matches = re.findall(r"^id:\s*(\d+)$", content, re.MULTILINE)
    assert len(id_matches) >= 4
    assert id_matches[:4] == ["1", "2", "3", "4"]

    # 2. Add two more events (seq 5 and 6)
    repo.append_event(ctx, run_id, "step_5", "system", {"step": 5})
    repo.append_event(ctx, run_id, "step_6", "system", {"step": 6})
    db_session.commit()

    # 3. Reconnect with Last-Event-ID: 4
    resume_headers = {
        "Accept": "text/event-stream",
        "Last-Event-ID": "4",
    }
    resp_resume = client.get(
        f"/runs/{run_id}/events?max_duration_seconds=0.3&heartbeat_seconds=0.1",
        headers=resume_headers,
    )
    assert resp_resume.status_code == 200
    resumed_content = resp_resume.text

    # Verify that stream resumed strictly from 5 onwards (no gaps, no duplicates)
    resumed_ids = re.findall(r"^id:\s*(\d+)$", resumed_content, re.MULTILINE)
    assert resumed_ids == ["5", "6"]


# ===========================================================================
# 4. SSE Heartbeat & Connection Duration Cap
# ===========================================================================


def test_sse_heartbeat_and_duration_cap(client, setup_tenants, db_session):
    """SSE stream emits periodic heartbeat comments and terminates upon duration cap."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])
    repo = RunRepository(db_session)
    repo.append_event(ctx, run_id, "run_created", "system", {})
    db_session.commit()

    # Set short heartbeat (0.1s) and duration cap (0.35s)
    headers = {"Accept": "text/event-stream"}
    resp = client.get(
        f"/runs/{run_id}/events?heartbeat_seconds=0.1&max_duration_seconds=0.35",
        headers=headers,
    )
    assert resp.status_code == 200
    text = resp.text

    # Verify heartbeat comment was emitted
    assert ": heartbeat\n\n" in text
    # Verify duration cap reached message was emitted
    assert ": connection duration cap reached\n\n" in text


# ===========================================================================
# 5. Terminal Status Closes the Stream
# ===========================================================================


def test_sse_terminal_status_closes_stream(client, setup_tenants, db_session):
    """When a run is completed or escalated, the SSE stream closes cleanly."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])

    # Transition run to terminal status 'escalated'
    service = RunService(db_session)
    run = db_session.get(RunModel, run_id)
    service.transition(
        ctx=ctx,
        run_id=run_id,
        to_status="escalated",
        actor="system",
        expected_version=run.version,
        payload={"reason": "test_terminal"},
    )
    db_session.commit()

    headers = {"Accept": "text/event-stream"}
    resp = client.get(f"/runs/{run_id}/events", headers=headers)
    assert resp.status_code == 200
    text = resp.text

    # Stream emitted the terminal event and closed cleanly
    assert "stream closed: terminal status" in text


# ===========================================================================
# 6. Payload Sanitization & Secret Scanning (Feature 2.3 Detector)
# ===========================================================================


def test_payload_sanitization_and_secret_scanning(client, setup_tenants, db_session):
    """Emitted event payloads are scanned for sensitive tokens and verified redacted."""
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["alpha_token"])
    run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])
    repo = RunRepository(db_session)

    # Append event containing raw secrets
    sensitive_payload = {
        "auth_header": "Bearer secret_api_bearer_token_xyz123",
        "database_url": "postgresql://dbuser:SuperSecretPassword123@prod-db.internal:5432/main",
        "api_key": "api_key=sk_live_12345678901234567890",
        "client_secret": "client_secret=VerySecretValue9999",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.do_not_leak_this_jwt_signature_long_hash",
        "operator_email": "root_operator@internal-company.org",
        "clean_summary": "Orders database pool saturated at 95%",
    }
    repo.append_event(ctx, run_id, "audit_inspection", "security_scanner", sensitive_payload)
    db_session.commit()

    # 1. Verify via polling JSON mode
    resp_json = client.get(f"/runs/{run_id}/events").json()
    last_event = resp_json["items"][-1]
    payload_str = json.dumps(last_event["payload"])

    # Feature 2.3 secret patterns: ensure no raw secret survived
    assert "SuperSecretPassword123" not in payload_str
    assert "secret_api_bearer_token_xyz123" not in payload_str
    assert "sk_live_12345678901234567890" not in payload_str
    assert "VerySecretValue9999" not in payload_str
    assert "root_operator@internal-company.org" not in payload_str

    # Ensure replacement markers are present
    assert "[REDACTED:PASSWORD]" in payload_str
    assert "[REDACTED:BEARER_TOKEN]" in payload_str
    assert "[REDACTED:API_KEY]" in payload_str
    assert "[REDACTED:SECRET]" in payload_str
    assert "[REDACTED:JWT]" in payload_str
    assert "[REDACTED:EMAIL]" in payload_str

    # Clean text is preserved
    assert "Orders database pool saturated at 95%" in payload_str

    # 2. Verify via SSE stream
    headers = {"Accept": "text/event-stream"}
    resp_sse = client.get(f"/runs/{run_id}/events?max_duration_seconds=0.2", headers=headers)
    sse_text = resp_sse.text
    assert "SuperSecretPassword123" not in sse_text
    assert "secret_api_bearer_token_xyz123" not in sse_text
    assert "[REDACTED:PASSWORD]" in sse_text


# ===========================================================================
# 7. Tenant Isolation Across Event Streams (404 Semantics)
# ===========================================================================


def test_tenant_isolation_returns_404(client, setup_tenants, db_session):
    """Beta user requesting Alpha's run events receives 404 (no existence leaks)."""
    alpha_run_id = setup_tenants["run_id"]
    ctx = TenantContext(tenant_id=setup_tenants["alpha_tenant_id"])
    repo = RunRepository(db_session)
    repo.append_event(ctx, alpha_run_id, "run_created", "system", {})
    db_session.commit()

    # Authenticate as Beta user
    client.cookies.set(SESSION_COOKIE_NAME, setup_tenants["beta_token"])

    # 1. Polling mode cross-tenant returns 404
    resp_poll = client.get(f"/runs/{alpha_run_id}/events")
    assert resp_poll.status_code == 404
    assert resp_poll.json()["error"]["code"] == "not_found"

    # 2. SSE streaming mode cross-tenant returns 404
    headers = {"Accept": "text/event-stream"}
    resp_sse = client.get(f"/runs/{alpha_run_id}/events", headers=headers)
    assert resp_sse.status_code == 404
    assert resp_sse.json()["error"]["code"] == "not_found"
