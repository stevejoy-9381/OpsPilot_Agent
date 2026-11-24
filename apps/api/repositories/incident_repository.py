"""Repository for the Incident aggregate.

Enforces:
1. TenantContext scoping on all operations.
2. Safe 404 on cross-tenant requests.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.models.tables import IncidentModel
from apps.api.repositories.base import BaseRepository


class IncidentRepository(BaseRepository):
    """Repository handling Incident persistence and retrieval."""

    def get_by_id(self, ctx: TenantContext, incident_id: UUID) -> IncidentModel:
        """Retrieves an incident by id within the tenant context.

        Raises NotFoundError if not found.
        """
        self.set_tenant_context(ctx)
        stmt = select(IncidentModel).where(
            IncidentModel.id == incident_id,
            IncidentModel.tenant_id == ctx.tenant_id,
        )
        incident = self.session.scalar(stmt)
        if incident is None:
            raise NotFoundError(f"Incident {incident_id} not found for tenant {ctx.tenant_id}")
        return incident

    def create(self, ctx: TenantContext, incident: IncidentModel) -> IncidentModel:
        """Persists a new incident within tenant context."""
        self.set_tenant_context(ctx)
        incident.tenant_id = ctx.tenant_id
        self.session.add(incident)
        self.session.flush()
        return incident

    def get_by_alert_id(self, ctx: TenantContext, alert_id: str) -> IncidentModel | None:
        """Retrieves an incident by alert_id within tenant context."""
        self.set_tenant_context(ctx)
        stmt = select(IncidentModel).where(
            IncidentModel.alert_id == alert_id,
            IncidentModel.tenant_id == ctx.tenant_id,
        )
        return self.session.scalar(stmt)
