"""Repository for the ActionPlan aggregate.

Enforces:
1. TenantContext scoping on all operations.
2. Read-only retrieval for review screens.
3. Safe 404 on cross-tenant requests.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.models.tables import ActionPlanModel
from apps.api.repositories.base import BaseRepository


class PlanRepository(BaseRepository):
    """Repository handling ActionPlan retrieval."""

    def get_by_id(self, ctx: TenantContext, plan_id: UUID) -> ActionPlanModel:
        """Retrieves an action plan by id within tenant context.

        Raises NotFoundError if not found or cross-tenant.
        """
        self.set_tenant_context(ctx)
        stmt = select(ActionPlanModel).where(
            ActionPlanModel.id == plan_id,
            ActionPlanModel.tenant_id == ctx.tenant_id,
        )
        plan = self.session.scalar(stmt)
        if plan is None:
            raise NotFoundError(f"Plan {plan_id} not found for tenant {ctx.tenant_id}")
        return plan
