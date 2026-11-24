"""Authentication routes: login, logout, me, and protected demonstration routes.

Enforces:
1. POST /auth/login:
   - Validates email + password against users table with Argon2 constant-time verification.
   - Generic error messages for nonexistent user or wrong password ("Invalid credentials").
   - Basic rate limiting (Redis backend with in-memory fallback).
   - Generates opaque random token, stores SHA-256 digest in sessions table.
   - Sets HttpOnly, SameSite=Lax (or Strict), Secure-in-prod cookie.
   - Sets double-submit csrftoken cookie.
   - Audit logging of success/failure (redacted, no passwords or tokens).
2. POST /auth/logout:
   - Revokes current session (sets revoked_at = now()).
   - Clears session cookies.
3. GET /auth/me:
   - Returns {user_id, tenant_id, role} derived strictly from session principal.
4. Protected review route:
   - Requires reviewer role.
   - Forbids tenant_id or reviewer_id in request payload (rejects with 400/422).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.auth.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from apps.api.auth.dependencies import (
    SESSION_COOKIE_NAME,
    Principal,
    get_current_principal,
    get_db_session,
    require_role,
)
from apps.api.auth.passwords import verify_password
from apps.api.auth.rate_limiter import login_rate_limiter
from apps.api.auth.tokens import generate_session_token, hash_session_token
from apps.api.models.tables import SessionModel, UserModel

logger = logging.getLogger("opspilot.audit.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_TTL_HOURS = 24


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    message: str
    csrf_token: str
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


class MeResponse(BaseModel):
    user_id: uuid.UUID | None
    tenant_id: uuid.UUID
    role: str
    is_service: bool


class ApprovalRequest(BaseModel):
    plan_id: uuid.UUID
    decision: str
    reason: str = ""

    # Reject any request body attempting to supply tenant_id or reviewer_id
    @model_validator(mode="before")
    @classmethod
    def reject_spoofed_identities(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict):
            if "tenant_id" in data or "reviewer_id" in data:
                raise ValueError(
                    "tenant_id and reviewer_id must not be supplied in request body; identity is derived from session"
                )
        return data


@router.post("/login", response_model=LoginResponse)
def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db_session),
) -> LoginResponse:
    """Authenticates user with email and password, setting HttpOnly session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    rate_limit_key = f"login:{client_ip}:{req.email.lower()}"

    # 1. Rate limiting check
    if login_rate_limiter.is_rate_limited(rate_limit_key):
        logger.warning(
            "AUDIT LOGIN FAILED: Rate limit exceeded for ip=%s email=%s",
            client_ip,
            req.email,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )

    # 2. Look up user by email
    stmt = select(UserModel).where(UserModel.email == req.email.lower().strip())
    user = session.scalar(stmt)

    # 3. Constant-time password verification with generic error response
    valid = False
    if user is not None:
        valid = verify_password(user.password_hash, req.password)

    if not valid:
        login_rate_limiter.record_attempt(rate_limit_key)
        logger.warning(
            "AUDIT LOGIN FAILED: Invalid credentials for ip=%s email=%s",
            client_ip,
            req.email,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Reset rate limit on successful authentication
    login_rate_limiter.reset(rate_limit_key)

    assert user is not None

    # 4. Create session record
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    session_record = SessionModel(
        token_hash=token_hash,
        user_id=user.id,
        tenant_id=user.tenant_id,
        expires_at=expires_at,
    )
    session.add(session_record)
    session.commit()

    # 5. Cookie configuration: HttpOnly, SameSite=Strict, Secure in production
    is_prod = os.getenv("ENVIRONMENT", "dev").lower() in ("prod", "production")

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="strict",
        secure=is_prod,
        expires=int(timedelta(hours=SESSION_TTL_HOURS).total_seconds()),
        path="/",
    )

    csrf_token = generate_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # Accessible by frontend scripts for double-submit header
        samesite="strict",
        secure=is_prod,
        path="/",
    )

    logger.info(
        "AUDIT LOGIN SUCCESS: user_id=%s tenant_id=%s role=%s ip=%s",
        user.id,
        user.tenant_id,
        user.role,
        client_ip,
    )

    return LoginResponse(
        message="Login successful",
        csrf_token=csrf_token,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_db_session),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, str]:
    """Revokes the current session and clears cookies."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if session_cookie:
        token_hash = hash_session_token(session_cookie)
        session.execute(
            update(SessionModel)
            .where(SessionModel.token_hash == token_hash)
            .values(revoked_at=datetime.now(timezone.utc))
        )
        session.commit()

    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")

    logger.info(
        "AUDIT LOGOUT: user_id=%s tenant_id=%s",
        principal.user_id,
        principal.tenant_id,
    )
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=MeResponse)
def get_me(principal: Principal = Depends(get_current_principal)) -> MeResponse:
    """Returns the authenticated principal identity."""
    return MeResponse(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        role=principal.role,
        is_service=principal.is_service,
    )


# Reviewer action endpoint for testing RBAC and body identity rejection
@router.post("/review/approve")
def submit_approval(
    req: ApprovalRequest,
    principal: Principal = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Submits an action plan approval. Requires reviewer or admin role."""
    return {
        "status": "approved",
        "plan_id": str(req.plan_id),
        "reviewer_id": str(principal.user_id),
        "tenant_id": str(principal.tenant_id),
        "decision": req.decision,
    }
