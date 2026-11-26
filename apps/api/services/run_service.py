"""Domain service orchestrating Run lifecycle transitions, error recording, and accounting.

Requirements:
1. RunService.transition(run_id, to_status, actor, expected_version, payload=None):
   - Optimistic locking: UPDATE ... WHERE id=:id AND version=:expected_version; zero rows
     => ConcurrentModification error (typed).
   - Validates against allowed transitions in application code too (fail fast with friendly error),
     Postgres trigger is the authoritative backstop.
   - Writes a run_events row in the SAME transaction with next monotonic seq.
2. RunService.record_error(run_id, error):
   - Writes last_error (typed) and an event.
   - Moves to failed or escalated only if the transition table allows it.
3. remaining_calls accounting:
   - decrement_calls(run_id, n=1): Atomic UPDATE with budget_exhausted / BudgetExhaustedError
     when it would go below zero.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import (
    ConcurrentModificationError,
    IllegalTransitionError,
)
from apps.api.models.tables import DiagnosisModel, RunEventModel, RunModel
from apps.api.repositories.run_repository import RunRepository

logger = logging.getLogger(__name__)


# Application-level cached transition table derived from docs/outcome_contract.md
# Provides fail-fast validation before hitting the database
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("queued", "investigating"),
        ("queued", "escalated"),
        ("queued", "failed"),
        ("investigating", "needs_review"),
        ("investigating", "completed"),
        ("investigating", "escalated"),
        ("investigating", "failed"),
        ("needs_review", "approved"),
        ("needs_review", "escalated"),
        ("approved", "executing"),
        ("approved", "escalated"),
        ("executing", "verifying"),
        ("executing", "escalated"),
        ("executing", "failed"),
        ("verifying", "completed"),
        ("verifying", "escalated"),
        ("escalated", "investigating"),
    }
)


class RunService:
    """Service orchestrating Run status state machine, events, and call budget."""

    def __init__(self, session: Session, run_repo: RunRepository | None = None) -> None:
        self.session = session
        self.run_repo = run_repo or RunRepository(session)

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        resolution: str | None = None,
        lease_token: UUID | None = None,
    ) -> tuple[RunModel, RunEventModel]:
        """Transitions a run to a new status under optimistic concurrency control.

        Steps:
        1. Optimistic atomic update conditioned on (id, tenant_id, version == expected_version).
           - If 0 rows updated, inspects current version and raises ConcurrentModificationError.
        2. Validates from_status -> to_status (fail fast against allowed transitions).
        3. Appends a run_event in the SAME transaction with gap-free monotonic seq.
        4. Returns the updated run and created event.

        Raises:
        - NotFoundError if run does not exist for tenant.
        - ConcurrentModificationError if version != expected_version.
        - IllegalTransitionError if transition is invalid.
        """
        if lease_token is not None:
            self.run_repo.verify_lease_or_raise(ctx, run_id, lease_token)

        # 1. Fetch current run inside transaction to determine from_status
        run = self.run_repo.get_by_id(ctx, run_id)
        from_status = run.status

        # 2. Check optimistic locking version FIRST so concurrent workers fail with ConcurrentModificationError
        if run.version != expected_version:
            raise ConcurrentModificationError(
                entity_id=str(run_id),
                expected_version=expected_version,
                actual_version=run.version,
            )

        # 3. Application-level validation against allowed transitions
        if (from_status, to_status) not in ALLOWED_TRANSITIONS:
            if not self.run_repo.is_transition_allowed(from_status, to_status):
                raise IllegalTransitionError(
                    from_status=from_status,
                    to_status=to_status,
                    message=(
                        f'Cannot transition run {run_id} from "{from_status}" '
                        f'to "{to_status}": illegal transition'
                    ),
                )

        # 4. Atomic optimistic update (in case another transaction committed between our read and update)
        if resolution is not None:
            updated_run = self.run_repo.update_status_optimistic(
                ctx=ctx,
                run_id=run_id,
                expected_version=expected_version,
                to_status=to_status,
                resolution=resolution,
                lease_token=lease_token,
            )
        else:
            updated_run = self.run_repo.update_status_optimistic(
                ctx=ctx,
                run_id=run_id,
                expected_version=expected_version,
                to_status=to_status,
                lease_token=lease_token,
            )

        # 5. Record audit event in the same transaction
        event_payload = {
            "from_status": from_status,
            "to_status": to_status,
            "expected_version": expected_version,
            **(payload or {}),
        }
        event = self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="status_transition",
            actor=actor,
            payload=event_payload,
        )

        logger.info(
            "Run %s transitioned from %s to %s by %s (version: %s -> %s)",
            run_id,
            from_status,
            to_status,
            actor,
            expected_version,
            updated_run.version,
        )
        return updated_run, event

    def record_error(
        self,
        ctx: TenantContext,
        run_id: UUID,
        error: dict[str, Any] | str,
        actor: str = "system",
        target_status: str | None = None,
        lease_token: UUID | None = None,
    ) -> RunModel:
        """Records a typed error payload and event on the run."""
        if lease_token is not None:
            self.run_repo.verify_lease_or_raise(ctx, run_id, lease_token)
        run = self.run_repo.get_by_id(ctx, run_id)
        error_payload = error if isinstance(error, dict) else {"message": str(error)}

        if target_status and target_status != run.status:
            is_allowed = (run.status, target_status) in ALLOWED_TRANSITIONS or (
                self.run_repo.is_transition_allowed(run.status, target_status)
            )
            if is_allowed:
                # Transition status and record error payload in the same transition
                updated_run, _ = self.transition(
                    ctx=ctx,
                    run_id=run_id,
                    to_status=target_status,
                    actor=actor,
                    expected_version=run.version,
                    payload={"reason": "error_recorded", "error": error_payload},
                )
                self.run_repo.record_error(ctx, run_id, error_payload)
                return self.run_repo.get_by_id(ctx, run_id)
            else:
                logger.warning(
                    "Error recorded on run %s, but transition from %s to %s is disallowed; keeping current status",
                    run_id,
                    run.status,
                    target_status,
                )

        # Update last_error on run without changing status
        self.run_repo.record_error(ctx, run_id, error_payload)

        # Record error event
        self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="error_recorded",
            actor=actor,
            payload={"error": error_payload, "current_status": run.status},
        )

        return self.run_repo.get_by_id(ctx, run_id)

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        lease_token: UUID | None = None,
    ) -> RunEventModel:
        """Appends an event to the run's audit log with a gap-free monotonic sequence."""
        if lease_token is not None:
            self.run_repo.verify_lease_or_raise(ctx, run_id, lease_token)
        return self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
        )

    def save_diagnosis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        diagnosis_data: dict[str, Any],
        status: str = "draft",
        lease_token: UUID | None = None,
    ) -> DiagnosisModel:
        """Persists or updates a diagnosis record and records an audit event."""
        if lease_token is not None:
            self.run_repo.verify_lease_or_raise(ctx, run_id, lease_token)
        diag = self.run_repo.save_diagnosis(
            ctx=ctx,
            run_id=run_id,
            diagnosis_data=diagnosis_data,
            status=status,
        )
        self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="diagnosis_persisted",
            actor="investigator",
            payload={
                "diagnosis_id": str(diag.id),
                "status": status,
                "root_cause_category": diag.root_cause_category,
                "root_cause_service": diag.root_cause_service,
                "mechanism": diag.mechanism,
                "evidence_ids": diag.evidence_ids,
            },
        )
        return diag

    def decrement_calls(
        self,
        ctx: TenantContext,
        run_id: UUID,
        n: int = 1,
        lease_token: UUID | None = None,
    ) -> int:
        """Decrements remaining tool calls by n atomically.

        Raises BudgetExhaustedError if remaining_calls < n.
        """
        if lease_token is not None:
            self.run_repo.verify_lease_or_raise(ctx, run_id, lease_token)
        return self.run_repo.decrement_calls(ctx, run_id, n=n)


class FencedRunService:
    """Worker-facing RunService wrapper requiring a valid lease_token on every write.

    Every state-changing call enforces `WHERE lease_token = :token`. If the run's
    lease was reclaimed by another worker, LeaseLostError is raised immediately.
    """

    def __init__(self, inner: RunService, lease_token: UUID) -> None:
        self._inner = inner
        self.lease_token = lease_token
        self.session = inner.session
        self.run_repo = inner.run_repo

    def transition(
        self,
        ctx: TenantContext,
        run_id: UUID,
        to_status: str,
        actor: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        resolution: str | None = None,
    ) -> tuple[RunModel, RunEventModel]:
        return self._inner.transition(
            ctx=ctx,
            run_id=run_id,
            to_status=to_status,
            actor=actor,
            expected_version=expected_version,
            payload=payload,
            resolution=resolution,
            lease_token=self.lease_token,
        )

    def record_error(
        self,
        ctx: TenantContext,
        run_id: UUID,
        error: dict[str, Any] | str,
        actor: str = "system",
        target_status: str | None = None,
    ) -> RunModel:
        return self._inner.record_error(
            ctx=ctx,
            run_id=run_id,
            error=error,
            actor=actor,
            target_status=target_status,
            lease_token=self.lease_token,
        )

    def append_event(
        self,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEventModel:
        return self._inner.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
            lease_token=self.lease_token,
        )

    def save_diagnosis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        diagnosis_data: dict[str, Any],
        status: str = "draft",
    ) -> DiagnosisModel:
        return self._inner.save_diagnosis(
            ctx=ctx,
            run_id=run_id,
            diagnosis_data=diagnosis_data,
            status=status,
            lease_token=self.lease_token,
        )

    def decrement_calls(self, ctx: TenantContext, run_id: UUID, n: int = 1) -> int:
        return self._inner.decrement_calls(
            ctx=ctx, run_id=run_id, n=n, lease_token=self.lease_token
        )
