"""Base repository providing tenant-aware transactional session management.

Enforces:
1. Every query and operation runs inside a transaction where 'app.tenant_id' is set (SET LOCAL).
2. No raw unparameterized SQL queries outside repositories.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.domain.context import TenantContext


class BaseRepository:
    """Base class for all aggregate repositories.

    Ensures that the repository session has the active tenant context
    bound via SET LOCAL app.tenant_id = :tenant_id.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def set_tenant_context(self, ctx: TenantContext) -> None:
        """Sets the PostgreSQL session configuration 'app.tenant_id' for this transaction."""
        self.session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(ctx.tenant_id)},
        )
