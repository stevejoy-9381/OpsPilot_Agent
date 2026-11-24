"""Principal definitions and FastAPI authentication dependencies.

Enforces:
1. Tenant and user identity are extracted strictly from the authenticated session (or service token),
   never from request bodies, query parameters, or URL paths.
2. Distinct principal types:
   - UserPrincipal: human operator logged in via session cookie (roles: viewer, reviewer, admin).
   - ServicePrincipal: worker / broker using scoped Bearer service token.
3. Worker identity can act on runs but is strictly forbidden from creating approvals.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.auth.csrf import verify_csrf
from apps.api.auth.tokens import hash_session_token
from apps.api.models.tables import UserModel

SESSION_COOKIE_NAME = "opspilot_session"
SERVICE_TOKEN_ENV_NAME = "OPSPILOT_SERVICE_TOKEN"

# Hierarchy of role permissions
ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 1,
    "reviewer": 2,
    "admin": 3,
}


@dataclass(frozen=True)
class Principal:
    """Authenticated caller identity."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    role: str
    is_service: bool = False

    def has_role(self, required_role: str) -> bool:
        """Checks if principal satisfies role requirement according to role hierarchy."""
        return ROLE_HIERARCHY.get(self.role, 0) >= ROLE_HIERARCHY.get(required_role, 0)


# Database session dependency for auth
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_db_session_factory() -> sessionmaker[Session]:
    global _engine, _session_factory
    if _engine is None:
        db_url = os.getenv("DATABASE_URL", "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot")
        if db_url.startswith("postgresql://"):
            db_url = "postgresql+psycopg://" + db_url[len("postgresql://") :]
        _engine = create_engine(db_url, pool_size=10, max_overflow=5)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    assert _session_factory is not None
    return _session_factory


def get_db_session() -> Generator[Session, None, None]:
    factory = get_db_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_current_principal(
    request: Request,
    session: Session = Depends(get_db_session),
) -> Principal:
    """Extracts and validates the current principal from session cookie or service token.

    Also runs CSRF checks on mutating HTTP requests.
    """
    auth_header = request.headers.get("Authorization")
    service_token = os.getenv(SERVICE_TOKEN_ENV_NAME)

    # 1. Check for Service Principal (Bearer token)
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if service_token and token == service_token:
            # Service token uses a dedicated system tenant scope or header
            tenant_header = request.headers.get("x-tenant-id")
            tenant_id = (
                uuid.UUID(tenant_header)
                if tenant_header
                else uuid.UUID("00000000-0000-0000-0000-000000000000")
            )
            verify_csrf(request, is_service_request=True)
            return Principal(
                tenant_id=tenant_id,
                user_id=None,
                role="worker",
                is_service=True,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )

    # 2. Check for User Session (HttpOnly cookie)
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    token_hash = hash_session_token(session_cookie)

    # Query app.sessions table
    from apps.api.models.tables import SessionModel

    stmt = (
        select(SessionModel, UserModel)
        .join(UserModel, UserModel.id == SessionModel.user_id)
        .where(
            SessionModel.token_hash == token_hash,
            SessionModel.revoked_at.is_(None),
            SessionModel.expires_at > datetime.now(timezone.utc),
        )
    )
    result = session.execute(stmt).first()
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session invalid or expired",
        )

    session_row, user_row = result

    # Enforce CSRF check for state-changing browser cookie requests
    verify_csrf(request, is_service_request=False)

    return Principal(
        tenant_id=user_row.tenant_id,
        user_id=user_row.id,
        role=user_row.role,
        is_service=False,
    )


def require_role(required_role: str) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing RBAC roles.

    Worker/service principals are permitted only for actions within their scope
    and are strictly forbidden from human review actions (such as approvals).
    """

    def _role_checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        # Worker principal cannot perform human roles (reviewer, admin)
        if principal.is_service:
            if required_role in ("reviewer", "admin"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Service principal cannot perform '{required_role}' actions",
                )
            return principal

        if not principal.has_role(required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{required_role}' required, user has '{principal.role}'",
            )
        return principal

    return _role_checker
