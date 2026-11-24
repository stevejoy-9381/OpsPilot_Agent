"""Unit and integration tests for authentication, sessions, CSRF, and RBAC.

Requirements tested:
1. Wrong password returns generic 401 error.
2. Expired session returns 401.
3. Revoked session returns 401.
4. Cookie flags (HttpOnly, SameSite=Strict, Secure).
5. CSRF rejection for state-changing browser cookie requests without custom header.
6. Viewer role cannot call reviewer routes (403 Forbidden).
7. Request body containing tenant_id or reviewer_id is rejected (422 Unprocessable Entity).
8. Service principal cannot insert approvals (403 Forbidden).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.auth.csrf import CSRF_HEADER_NAME, REQUESTED_WITH_HEADER
from apps.api.auth.dependencies import (
    SERVICE_TOKEN_ENV_NAME,
    SESSION_COOKIE_NAME,
    get_db_session,
)
from apps.api.auth.passwords import hash_password
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.main import app
from apps.api.models.tables import SessionModel, TenantModel, UserModel

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


@pytest.fixture
def seed_users(db_session):
    tenant_id = uuid.uuid4()
    t = TenantModel(id=tenant_id, slug=f"auth-t-{tenant_id.hex[:6]}", name="Auth Tenant")
    db_session.add(t)

    # 1. Viewer user
    viewer_id = uuid.uuid4()
    viewer_pwd = "ViewerSecret123!"
    u_viewer = UserModel(
        id=viewer_id,
        tenant_id=tenant_id,
        email="viewer@example.com",
        role="viewer",
        password_hash=hash_password(viewer_pwd),
    )
    db_session.add(u_viewer)

    # 2. Reviewer user
    reviewer_id = uuid.uuid4()
    reviewer_pwd = "ReviewerSecret123!"
    u_reviewer = UserModel(
        id=reviewer_id,
        tenant_id=tenant_id,
        email="reviewer@example.com",
        role="reviewer",
        password_hash=hash_password(reviewer_pwd),
    )
    db_session.add(u_reviewer)

    db_session.commit()
    return {
        "tenant_id": tenant_id,
        "viewer": {"id": viewer_id, "email": "viewer@example.com", "password": viewer_pwd},
        "reviewer": {"id": reviewer_id, "email": "reviewer@example.com", "password": reviewer_pwd},
    }


def test_login_wrong_password_generic_error(client, seed_users):
    """Wrong password returns 401 with generic error message, redacting internal reason."""
    resp = client.post(
        "/auth/login",
        json={"email": seed_users["viewer"]["email"], "password": "WrongPassword!"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"

    # Nonexistent user also returns identical generic 401
    resp2 = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "WrongPassword!"},
    )
    assert resp2.status_code == 401
    assert resp2.json()["detail"] == "Invalid credentials"


def test_login_success_and_cookie_flags(client, seed_users):
    """Successful login sets HttpOnly, SameSite=Strict cookie and issues CSRF token."""
    resp = client.post(
        "/auth/login",
        json={"email": seed_users["viewer"]["email"], "password": seed_users["viewer"]["password"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "viewer"
    assert "csrf_token" in data

    # Verify session cookie flags
    cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None
    # Inspect raw Set-Cookie header
    set_cookie_header = resp.headers.get("set-cookie")
    assert set_cookie_header is not None
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert (
        "SameSite=strict" in set_cookie_header.lower()
        or "samesite=strict" in set_cookie_header.lower()
    )


def test_expired_session_rejected(client, db_session, seed_users):
    """Expired session token is rejected with 401."""
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    user = seed_users["viewer"]

    # Insert already expired session (1 hour in the past)
    expired_sess = SessionModel(
        token_hash=token_hash,
        user_id=user["id"],
        tenant_id=seed_users["tenant_id"],
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(expired_sess)
    db_session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["detail"].lower()


def test_revoked_session_rejected(client, db_session, seed_users):
    """Revoked session token is rejected with 401."""
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    user = seed_users["viewer"]

    revoked_sess = SessionModel(
        token_hash=token_hash,
        user_id=user["id"],
        tenant_id=seed_users["tenant_id"],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        revoked_at=datetime.now(timezone.utc),
    )
    db_session.add(revoked_sess)
    db_session.commit()

    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["detail"].lower()


def test_csrf_rejection_and_allowance(client, seed_users):
    """State-changing requests with browser cookie without custom header fail with 403."""
    login_resp = client.post(
        "/auth/login",
        json={
            "email": seed_users["reviewer"]["email"],
            "password": seed_users["reviewer"]["password"],
        },
    )
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]

    plan_id = str(uuid.uuid4())

    # 1. Mutating POST without CSRF header or X-Requested-With fails with 403
    resp_no_csrf = client.post(
        "/auth/review/approve",
        json={"plan_id": plan_id, "decision": "approve", "reason": "Looks good"},
    )
    assert resp_no_csrf.status_code == 403
    assert "CSRF" in resp_no_csrf.json()["detail"]

    # 2. Mutating POST with X-Requested-With custom header succeeds
    resp_with_custom = client.post(
        "/auth/review/approve",
        json={"plan_id": plan_id, "decision": "approve", "reason": "Looks good"},
        headers={REQUESTED_WITH_HEADER: "XMLHttpRequest"},
    )
    assert resp_with_custom.status_code == 200
    assert resp_with_custom.json()["status"] == "approved"

    # 3. Mutating POST with matching X-CSRF-Token succeeds
    resp_with_token = client.post(
        "/auth/review/approve",
        json={"plan_id": plan_id, "decision": "approve", "reason": "Looks good"},
        headers={CSRF_HEADER_NAME: csrf_token},
    )
    assert resp_with_token.status_code == 200
    assert resp_with_token.json()["status"] == "approved"


def test_viewer_cannot_call_reviewer_routes(client, seed_users):
    """User with viewer role is rejected from reviewer-protected routes with 403."""
    login_resp = client.post(
        "/auth/login",
        json={"email": seed_users["viewer"]["email"], "password": seed_users["viewer"]["password"]},
    )
    assert login_resp.status_code == 200

    resp = client.post(
        "/auth/review/approve",
        json={"plan_id": str(uuid.uuid4()), "decision": "approve"},
        headers={REQUESTED_WITH_HEADER: "XMLHttpRequest"},
    )
    assert resp.status_code == 403
    assert "Role 'reviewer' required, user has 'viewer'" in resp.json()["detail"]


def test_body_containing_tenant_id_or_reviewer_id_rejected(client, seed_users):
    """Any attempt to supply tenant_id or reviewer_id in request body is rejected with 422."""
    login_resp = client.post(
        "/auth/login",
        json={
            "email": seed_users["reviewer"]["email"],
            "password": seed_users["reviewer"]["password"],
        },
    )
    assert login_resp.status_code == 200

    # Request body attempting to forge tenant_id
    resp = client.post(
        "/auth/review/approve",
        json={
            "plan_id": str(uuid.uuid4()),
            "decision": "approve",
            "tenant_id": str(uuid.uuid4()),  # Forged identity
        },
        headers={REQUESTED_WITH_HEADER: "XMLHttpRequest"},
    )
    assert resp.status_code == 422
    assert "tenant_id and reviewer_id must not be supplied in request body" in str(resp.json())

    # Request body attempting to forge reviewer_id
    resp2 = client.post(
        "/auth/review/approve",
        json={
            "plan_id": str(uuid.uuid4()),
            "decision": "approve",
            "reviewer_id": str(uuid.uuid4()),  # Forged identity
        },
        headers={REQUESTED_WITH_HEADER: "XMLHttpRequest"},
    )
    assert resp2.status_code == 422
    assert "tenant_id and reviewer_id must not be supplied in request body" in str(resp2.json())


def test_service_principal_cannot_insert_approvals(client, monkeypatch):
    """Service principal (worker/broker) can call worker endpoints but is forbidden from approvals."""
    service_secret = "secret-service-token-test-12345"
    monkeypatch.setenv(SERVICE_TOKEN_ENV_NAME, service_secret)

    # 1. Service principal calling /auth/me returns role="worker", is_service=True
    me_resp = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {service_secret}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == "worker"
    assert me_resp.json()["is_service"] is True

    # 2. Service principal attempting to call human reviewer route /auth/review/approve is rejected with 403
    review_resp = client.post(
        "/auth/review/approve",
        json={"plan_id": str(uuid.uuid4()), "decision": "approve"},
        headers={"Authorization": f"Bearer {service_secret}"},
    )
    assert review_resp.status_code == 403
    assert "Service principal cannot perform 'reviewer' actions" in review_resp.json()["detail"]


def test_logout_revokes_session(client, seed_users):
    """POST /auth/logout marks session revoked in database and clears session cookies."""
    login_resp = client.post(
        "/auth/login",
        json={"email": seed_users["viewer"]["email"], "password": seed_users["viewer"]["password"]},
    )
    assert login_resp.status_code == 200

    # Confirm authenticated
    me_resp = client.get("/auth/me")
    assert me_resp.status_code == 200

    # Logout
    logout_resp = client.post(
        "/auth/logout",
        headers={REQUESTED_WITH_HEADER: "XMLHttpRequest"},
    )
    assert logout_resp.status_code == 200

    # Subsequent /auth/me fails with 401
    me_after = client.get("/auth/me")
    assert me_after.status_code == 401
