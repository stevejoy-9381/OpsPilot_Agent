"""Domain error hierarchy for OpsPilot API and services.

These typed exceptions represent domain-level invariant violations and operational
errors that can be mapped directly to HTTP status codes at the API transport layer:
- NotFound -> 404
- Forbidden -> 403
- IllegalTransition -> 400 / 422
- ConcurrentModification -> 409
- IdempotencyConflict -> 409
- BudgetExhausted -> 429
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all OpsPilot domain exceptions."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    """Raised when an aggregate or entity cannot be found within the tenant scope."""


class ForbiddenError(DomainError):
    """Raised when a tenant or actor is forbidden from accessing or acting on a resource."""


class IllegalTransitionError(DomainError):
    """Raised when an entity state transition is disallowed by the outcome contract."""

    def __init__(
        self,
        from_status: str,
        to_status: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        msg = message or f'Illegal status transition from "{from_status}" to "{to_status}"'
        extra = {"from_status": from_status, "to_status": to_status}
        if details:
            extra.update(details)
        super().__init__(msg, details=extra)
        self.from_status = from_status
        self.to_status = to_status


class ConcurrentModificationError(DomainError):
    """Raised when an optimistic concurrency check fails (e.g. expected_version mismatch)."""

    def __init__(
        self,
        entity_id: str,
        expected_version: int,
        actual_version: int | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        msg = (
            message
            or f"Concurrent modification on entity {entity_id}: expected version {expected_version}"
            + (f", but found version {actual_version}" if actual_version is not None else "")
        )
        extra = {
            "entity_id": str(entity_id),
            "expected_version": expected_version,
            "actual_version": actual_version,
        }
        if details:
            extra.update(details)
        super().__init__(msg, details=extra)
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class IdempotencyConflictError(DomainError):
    """Raised when an operation conflicts with an existing idempotent execution or token."""


class BudgetExhaustedError(DomainError):
    """Raised when a run's remaining tool call budget is exhausted."""

    def __init__(
        self,
        run_id: str,
        remaining_calls: int,
        requested_calls: int = 1,
        message: str | None = None,
    ) -> None:
        msg = (
            message
            or f"Tool call budget exhausted for run {run_id}: remaining {remaining_calls}, requested {requested_calls}"
        )
        super().__init__(
            msg,
            details={
                "run_id": str(run_id),
                "remaining_calls": remaining_calls,
                "requested_calls": requested_calls,
            },
        )
        self.run_id = run_id
        self.remaining_calls = remaining_calls
        self.requested_calls = requested_calls


class PlanHashMismatchError(DomainError):
    """Raised when submitted_plan_hash does not match stored action_plans.plan_hash."""


class PlanExpiredError(DomainError):
    """Raised when an action plan has passed its expires_at deadline."""


class StaleTargetError(DomainError):
    """Raised when the live target version differs from plan.expected_version."""


class AlreadyDecidedError(DomainError):
    """Raised when a plan has already been decided."""


class DecisionConflictError(DomainError):
    """Raised when the same reviewer submits a conflicting decision for a plan."""


class PlanSupersededError(DomainError):
    """Raised when a plan was superseded by a newer proposal."""


class InvalidPlanStatusError(DomainError):
    """Raised when a plan is not in the expected 'proposed' state."""


class PlanNotProposedError(InvalidPlanStatusError):
    """Raised when a plan is not in 'proposed' status."""


class RunNotInReviewError(DomainError):
    """Raised when the owning run is not in 'needs_review' state."""


class SeparationOfDutiesError(DomainError):
    """Raised when a reviewer attempts to approve a run they created under 4-eyes policy."""


class TargetStateUnavailableError(DomainError):
    """Raised when live target state cannot be read or is stale prior to approval."""


TargetUnavailableError = TargetStateUnavailableError


class LeaseLostError(DomainError):
    """Raised when a worker's lease_token is no longer valid (fencing check failed)."""
