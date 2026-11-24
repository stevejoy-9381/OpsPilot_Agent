"""Repository for the Run aggregate and associated run_events.

Enforces:
1. TenantContext scoping on all operations.
2. Optimistic locking on run state updates via version checks.
3. Monotonic, gap-free seq numbers for run_events within a run.
4. Atomic accounting on remaining_calls with negative balance prevention.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    BudgetExhaustedError,
    ConcurrentModificationError,
    NotFoundError,
)
from apps.api.models.tables import (
    ActionPlanModel,
    AllowedTransitionModel,
    DiagnosisModel,
    HypothesisModel,
    ObservationModel,
    RunEventModel,
    RunModel,
)
from apps.api.repositories.base import BaseRepository


class RunRepository(BaseRepository):
    """Repository handling Run persistence, event appending, and call accounting."""

    def get_by_id(self, ctx: TenantContext, run_id: UUID) -> RunModel:
        """Retrieves a run by id within the tenant context.

        Raises NotFoundError if not found.
        """
        self.set_tenant_context(ctx)
        stmt = select(RunModel).where(
            RunModel.id == run_id,
            RunModel.tenant_id == ctx.tenant_id,
        )
        run = self.session.scalar(stmt)
        if run is None:
            raise NotFoundError(f"Run {run_id} not found for tenant {ctx.tenant_id}")
        return run

    def create(self, ctx: TenantContext, run: RunModel) -> RunModel:
        """Persists a new run."""
        self.set_tenant_context(ctx)
        run.tenant_id = ctx.tenant_id
        self.session.add(run)
        self.session.flush()
        self.session.refresh(run)
        return run

    def update_status_optimistic(
        self,
        ctx: TenantContext,
        run_id: UUID,
        expected_version: int,
        to_status: str,
        finished: bool = False,
    ) -> RunModel:
        """Updates run status using optimistic locking.

        Checks that version == expected_version. If 0 rows updated, raises
        ConcurrentModificationError.

        Note: PostgreSQL trigger trg_runs_enforce_transition automatically
        increments version, updates updated_at, and sets finished_at when entering
        terminal statuses.
        """
        self.set_tenant_context(ctx)

        # Execute atomic update conditioned on id, tenant_id, and expected_version
        stmt = (
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.tenant_id == ctx.tenant_id,
                RunModel.version == expected_version,
            )
            .values(status=to_status)
            .returning(RunModel.id, RunModel.version, RunModel.status)
        )
        result = self.session.execute(stmt).fetchone()

        if result is None:
            # Check whether run exists to give precise actual version
            current = self.session.scalar(
                select(RunModel.version).where(
                    RunModel.id == run_id,
                    RunModel.tenant_id == ctx.tenant_id,
                )
            )
            if current is None:
                raise NotFoundError(f"Run {run_id} not found for tenant {ctx.tenant_id}")
            raise ConcurrentModificationError(
                entity_id=str(run_id),
                expected_version=expected_version,
                actual_version=current,
            )

        self.session.flush()
        # Re-fetch updated run within current transaction and refresh DB-trigger generated fields
        run = self.get_by_id(ctx, run_id)
        self.session.refresh(run)
        return run

    def record_error(
        self,
        ctx: TenantContext,
        run_id: UUID,
        error_payload: dict[str, Any],
    ) -> RunModel:
        """Records typed last_error JSON on the run without changing status."""
        self.set_tenant_context(ctx)
        run = self.get_by_id(ctx, run_id)
        run.last_error = error_payload
        self.session.flush()
        self.session.refresh(run)
        return run

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEventModel:
        """Appends a new event with the next monotonically increasing seq number.

        Uses SELECT COALESCE(MAX(seq), 0) + 1 FROM app.run_events WHERE run_id = :run_id
        inside the transaction with row locking to ensure gap-free monotonicity.
        """
        self.set_tenant_context(ctx)

        # Acquire lock on the run row to serialize event creation for this run
        self.session.execute(
            select(RunModel.id)
            .where(RunModel.id == run_id, RunModel.tenant_id == ctx.tenant_id)
            .with_for_update()
        )

        # Compute next monotonic seq
        next_seq_stmt = select(func.coalesce(func.max(RunEventModel.seq), 0) + 1).where(
            RunEventModel.run_id == run_id,
            RunEventModel.tenant_id == ctx.tenant_id,
        )
        next_seq = self.session.scalar(next_seq_stmt) or 1

        event = RunEventModel(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            seq=next_seq,
            type=event_type,
            actor=actor,
            payload=payload or {},
        )
        self.session.add(event)
        self.session.flush()
        try:
            from apps.api.services.event_broadcaster import broadcaster

            broadcaster.publish_event(run_id, next_seq)
        except Exception:
            pass
        return event

    def decrement_calls(
        self,
        ctx: TenantContext,
        run_id: UUID,
        n: int = 1,
    ) -> int:
        """Atomically decrements remaining_calls by n.

        Guaranteed never to go below zero (enforced by DB check and atomic UPDATE).
        Returns the new remaining_calls count.
        Raises BudgetExhaustedError if remaining_calls < n.
        """
        self.set_tenant_context(ctx)

        stmt = (
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.tenant_id == ctx.tenant_id,
                RunModel.remaining_calls >= n,
            )
            .values(remaining_calls=RunModel.remaining_calls - n)
            .returning(RunModel.remaining_calls)
        )
        result = self.session.execute(stmt).fetchone()

        if result is None:
            # Inspect current balance to report exact numbers
            current = self.session.scalar(
                select(RunModel.remaining_calls).where(
                    RunModel.id == run_id,
                    RunModel.tenant_id == ctx.tenant_id,
                )
            )
            if current is None:
                raise NotFoundError(f"Run {run_id} not found for tenant {ctx.tenant_id}")
            raise BudgetExhaustedError(
                run_id=str(run_id),
                remaining_calls=current,
                requested_calls=n,
            )

        self.session.flush()
        return int(result[0])

    def is_transition_allowed(
        self,
        from_status: str,
        to_status: str,
    ) -> bool:
        """Checks whether a transition is allowed according to app.allowed_transitions."""
        stmt = select(AllowedTransitionModel).where(
            AllowedTransitionModel.from_status == from_status,
            AllowedTransitionModel.to_status == to_status,
        )
        return self.session.scalar(stmt) is not None

    def get_by_idempotency_key(
        self,
        ctx: TenantContext,
        key: str,
    ) -> RunModel | None:
        """Retrieves a run by tenant and idempotency key."""
        self.set_tenant_context(ctx)
        stmt = select(RunModel).where(
            RunModel.tenant_id == ctx.tenant_id,
            RunModel.idempotency_key == key,
        )
        return self.session.scalar(stmt)

    def list_runs(
        self,
        ctx: TenantContext,
        status: str | None = None,
        incident_id: UUID | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[RunModel], str | None, bool]:
        """Lists runs scoped to tenant with stable cursor pagination."""
        self.set_tenant_context(ctx)
        stmt = select(RunModel).where(RunModel.tenant_id == ctx.tenant_id)

        if status:
            stmt = stmt.where(RunModel.status == status)
        if incident_id:
            stmt = stmt.where(RunModel.incident_id == incident_id)

        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode("utf-8")
                parts = decoded.split(",", 1)
                cursor_created_at = datetime.fromisoformat(parts[0])
                cursor_id = UUID(parts[1])
                stmt = stmt.where(
                    or_(
                        RunModel.created_at < cursor_created_at,
                        and_(
                            RunModel.created_at == cursor_created_at,
                            RunModel.id < cursor_id,
                        ),
                    )
                )
            except Exception:
                pass

        stmt = stmt.order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit + 1)
        rows = list(self.session.scalars(stmt).all())

        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            raw_cursor = f"{last.created_at.isoformat()},{last.id}"
            next_cursor = base64.b64encode(raw_cursor.encode("utf-8")).decode("utf-8")

        return items, next_cursor, has_more

    def get_run_evidence_ids(self, ctx: TenantContext, run_id: UUID) -> list[str]:
        """Returns all evidence IDs collected for the run."""
        self.set_tenant_context(ctx)
        stmt = (
            select(ObservationModel.evidence_id)
            .where(
                ObservationModel.run_id == run_id,
                ObservationModel.tenant_id == ctx.tenant_id,
            )
            .order_by(ObservationModel.captured_at.asc())
        )
        return list(self.session.scalars(stmt).all())

    def get_run_hypotheses(self, ctx: TenantContext, run_id: UUID) -> list[HypothesisModel]:
        """Returns all hypotheses formulated for the run."""
        self.set_tenant_context(ctx)
        stmt = (
            select(HypothesisModel)
            .where(
                HypothesisModel.run_id == run_id,
                HypothesisModel.tenant_id == ctx.tenant_id,
            )
            .order_by(HypothesisModel.created_at.asc())
        )
        return list(self.session.scalars(stmt).all())

    def get_run_proposed_plan_id(self, ctx: TenantContext, run_id: UUID) -> UUID | None:
        """Returns the ID of the newest proposed or approved action plan for the run."""
        self.set_tenant_context(ctx)
        stmt = (
            select(ActionPlanModel.id)
            .where(
                ActionPlanModel.run_id == run_id,
                ActionPlanModel.tenant_id == ctx.tenant_id,
            )
            .order_by(ActionPlanModel.created_at.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def get_run_diagnosis(self, ctx: TenantContext, run_id: UUID) -> DiagnosisModel | None:
        """Returns the diagnosis for the run if one exists."""
        self.set_tenant_context(ctx)
        stmt = select(DiagnosisModel).where(
            DiagnosisModel.run_id == run_id,
            DiagnosisModel.tenant_id == ctx.tenant_id,
        )
        return self.session.scalar(stmt)

    def get_evidence_list(self, ctx: TenantContext, run_id: UUID) -> list[ObservationModel]:
        """Returns all observations for the run, ordered by captured_at."""
        self.set_tenant_context(ctx)
        # Ensure run exists for tenant first to avoid leaking existence
        self.get_by_id(ctx, run_id)
        stmt = (
            select(ObservationModel)
            .where(
                ObservationModel.run_id == run_id,
                ObservationModel.tenant_id == ctx.tenant_id,
            )
            .order_by(ObservationModel.captured_at.asc())
        )
        return list(self.session.scalars(stmt).all())

    def get_evidence_by_id(
        self, ctx: TenantContext, run_id: UUID, evidence_id: str
    ) -> ObservationModel:
        """Returns a single observation by evidence_id, scoped to run and tenant."""
        self.set_tenant_context(ctx)
        # Ensure run exists for tenant first
        self.get_by_id(ctx, run_id)
        stmt = select(ObservationModel).where(
            ObservationModel.run_id == run_id,
            ObservationModel.evidence_id == evidence_id,
            ObservationModel.tenant_id == ctx.tenant_id,
        )
        obs = self.session.scalar(stmt)
        if obs is None:
            raise NotFoundError(
                f"Evidence '{evidence_id}' not found for run {run_id} in tenant {ctx.tenant_id}"
            )
        return obs

    def get_run_events(
        self,
        ctx: TenantContext,
        run_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[RunEventModel]:
        """Retrieves ordered run events after a sequence number strictly scoped to tenant."""
        self.set_tenant_context(ctx)
        # Ensure run exists for tenant first to avoid leaking existence across tenants
        self.get_by_id(ctx, run_id)

        stmt = (
            select(RunEventModel)
            .where(
                RunEventModel.run_id == run_id,
                RunEventModel.tenant_id == ctx.tenant_id,
                RunEventModel.seq > after_seq,
            )
            .order_by(RunEventModel.seq.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        return list(self.session.scalars(stmt).all())


