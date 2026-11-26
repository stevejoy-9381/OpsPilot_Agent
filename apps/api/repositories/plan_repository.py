"""Repository for the ActionPlan aggregate.

Enforces:
1. TenantContext scoping on all operations.
2. Atomic plan creation + prior-plan superseding + plan_created event + run transition.
3. DB-clock expiry checks and background-safe expire_overdue_plans().
4. Read-only retrieval for review screens (404 on cross-tenant requests).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.models.tables import (
    ActionPlanModel,
    DiagnosisModel,
    HypothesisModel,
    ObservationModel,
    RunModel,
)
from apps.api.repositories.base import BaseRepository
from shared.plans import ActionPlan, PlanStatus, plan_summary


class PlanRepository(BaseRepository):
    """Repository handling ActionPlan persistence, expiry, and retrieval."""

    def db_now(self) -> datetime:
        """Returns the authoritative database clock timestamp in UTC."""
        raw_dt: Any = self.session.scalar(select(func.clock_timestamp()))
        if isinstance(raw_dt, str):
            raw_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        if not isinstance(raw_dt, datetime):
            raise RuntimeError("Failed to read database clock_timestamp()")
        if raw_dt.tzinfo is None:
            return raw_dt.replace(tzinfo=timezone.utc)
        return raw_dt.astimezone(timezone.utc)

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

    def get_review_data(
        self, ctx: TenantContext, plan_id: UUID
    ) -> tuple[ActionPlanModel, DiagnosisModel | None, list[ObservationModel], list[HypothesisModel]]:
        """Loads plan, diagnosis, observations, and hypotheses scoped by tenant."""
        plan = self.get_by_id(ctx, plan_id)
        diag = self.session.scalar(
            select(DiagnosisModel).where(
                DiagnosisModel.run_id == plan.run_id,
                DiagnosisModel.tenant_id == ctx.tenant_id,
            )
        )
        observations = list(
            self.session.scalars(
                select(ObservationModel)
                .where(
                    ObservationModel.run_id == plan.run_id,
                    ObservationModel.tenant_id == ctx.tenant_id,
                )
                .order_by(ObservationModel.captured_at.asc())
            ).all()
        )
        hypotheses = list(
            self.session.scalars(
                select(HypothesisModel)
                .where(
                    HypothesisModel.run_id == plan.run_id,
                    HypothesisModel.tenant_id == ctx.tenant_id,
                )
                .order_by(HypothesisModel.created_at.asc())
            ).all()
        )
        return plan, diag, observations, hypotheses

    def create(
        self,
        ctx: TenantContext,
        plan: ActionPlan,
        *,
        run_service: Any,
        expected_run_version: int,
        policy_version: str | None = None,
    ) -> ActionPlanModel:
        """Atomically creates an ActionPlan, supersedes prior proposed plans, emits
        plan_created, and transitions the run to needs_review under optimistic locking.

        All-or-nothing: any failure (including ConcurrentModificationError or DB trigger
        rejection) rolls back the savepoint so nothing persists.
        """
        self.set_tenant_context(ctx)
        run_uuid = UUID(str(plan.run_id))
        plan_uuid = _parse_plan_uuid(plan.plan_id)

        with self.session.begin_nested():
            # 1. Lock owning run row to serialize concurrent proposals for the same run
            run_row = self.session.scalar(
                select(RunModel)
                .where(
                    RunModel.id == run_uuid,
                    RunModel.tenant_id == ctx.tenant_id,
                )
                .with_for_update()
            )
            if run_row is None:
                raise NotFoundError(f"Run {run_uuid} not found for tenant {ctx.tenant_id}")

            # 2. Supersede any prior non-terminal ('proposed') plan for this run
            self.session.execute(
                update(ActionPlanModel)
                .where(
                    ActionPlanModel.run_id == run_uuid,
                    ActionPlanModel.tenant_id == ctx.tenant_id,
                    ActionPlanModel.status == PlanStatus.PROPOSED.value,
                )
                .values(status=PlanStatus.SUPERSEDED.value)
            )

            # 3. Insert new plan with status 'proposed'
            model = ActionPlanModel(
                id=plan_uuid,
                run_id=run_uuid,
                tenant_id=ctx.tenant_id,
                action=plan.action.value,
                service=plan.service,
                target_id=plan.target_id,
                expected_version=plan.expected_version,
                rollback_to_version=plan.rollback_to_version,
                rationale_evidence_ids=list(plan.rationale_evidence_ids),
                rollback_route=plan.rollback_route,
                plan_hash=plan.plan_hash,
                policy_version=policy_version,
                status=PlanStatus.PROPOSED.value,
                notes=plan.note,
                expires_at=plan.expires_at,
            )
            self.session.add(model)
            self.session.flush()

            # 4. Emit plan_created event (summary only, no raw note text)
            summary = plan_summary(plan)
            if policy_version is not None:
                summary["policy_version"] = policy_version
            run_service.append_event(
                ctx=ctx,
                run_id=run_uuid,
                event_type="plan_created",
                actor="investigator_propose",
                payload=summary,
            )

            # 5. Transition run investigating -> needs_review under optimistic locking
            run_service.transition(
                ctx=ctx,
                run_id=run_uuid,
                to_status="needs_review",
                actor="investigator_propose",
                expected_version=expected_run_version,
                payload=summary,
            )

            self.session.flush()
            return model

    def expire_overdue_plans(
        self,
        ctx: TenantContext | None = None,
        *,
        run_service: Any | None = None,
    ) -> list[UUID]:
        """Marks overdue proposed plans (expires_at <= DB clock_timestamp()) as expired.

        Background-safe helper: uses FOR UPDATE SKIP LOCKED on PostgreSQL and emits
        plan_expired events. Feature 8 approval also checks expiry directly.
        """
        if ctx is not None:
            self.set_tenant_context(ctx)

        stmt = (
            select(ActionPlanModel)
            .where(
                ActionPlanModel.status == PlanStatus.PROPOSED.value,
                ActionPlanModel.expires_at <= func.clock_timestamp(),
            )
            .with_for_update(skip_locked=True)
        )
        if ctx is not None:
            stmt = stmt.where(ActionPlanModel.tenant_id == ctx.tenant_id)

        overdue = list(self.session.scalars(stmt).all())
        expired_ids: list[UUID] = []
        for plan_row in overdue:
            plan_row.status = PlanStatus.EXPIRED.value
            expired_ids.append(plan_row.id)
            if run_service is not None:
                plan_ctx = TenantContext(tenant_id=plan_row.tenant_id)
                self.set_tenant_context(plan_ctx)
                run_row = self.session.scalar(
                    select(RunModel)
                    .where(
                        RunModel.id == plan_row.run_id,
                        RunModel.tenant_id == plan_row.tenant_id,
                    )
                    .with_for_update()
                )
                if run_row is not None and run_row.status == "needs_review":
                    run_service.transition(
                        ctx=plan_ctx,
                        run_id=plan_row.run_id,
                        to_status="escalated",
                        actor="system:reaper",
                        expected_version=run_row.version,
                        payload={"plan_id": str(plan_row.id), "reason": "approval_expired"},
                    )
                    run_service.record_error(
                        ctx=plan_ctx,
                        run_id=plan_row.run_id,
                        error={
                            "code": "approval_expired",
                            "message": "Plan expired; ask for a new investigation.",
                            "details": {"plan_id": str(plan_row.id)},
                        },
                        actor="system:reaper",
                    )
                run_service.append_event(
                    ctx=plan_ctx,
                    run_id=plan_row.run_id,
                    event_type="plan_expired",
                    actor="system:reaper",
                    payload={"plan_id": str(plan_row.id), "plan_hash": plan_row.plan_hash},
                )
        if expired_ids:
            self.session.flush()
        return expired_ids


def _parse_plan_uuid(raw_id: str) -> UUID:
    try:
        return UUID(raw_id)
    except ValueError:
        return uuid4()
