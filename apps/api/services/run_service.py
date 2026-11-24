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
from apps.api.models.tables import RunEventModel, RunModel
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
        updated_run = self.run_repo.update_status_optimistic(
            ctx=ctx,
            run_id=run_id,
            expected_version=expected_version,
            to_status=to_status,
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
    ) -> RunModel:
        """Records a typed error payload and event on the run.

        If target_status (e.g. 'escalated' or 'failed') is specified, attempts
        to transition to it ONLY IF the transition table permits. If the transition
        is not permitted from the current status, the error is still recorded and
        the run remains in its current status.
        """
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

    def decrement_calls(
        self,
        ctx: TenantContext,
        run_id: UUID,
        n: int = 1,
    ) -> int:
        """Decrements remaining tool calls by n atomically.

        Raises BudgetExhaustedError if remaining_calls < n.
        """
        return self.run_repo.decrement_calls(ctx, run_id, n=n)
