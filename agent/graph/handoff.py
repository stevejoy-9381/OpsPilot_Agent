"""agent/graph/handoff.py

HandoffPort interface and default stub implementation for investigator conclusion handoff.

Provides an explicit extension point for Feature 6 (Policy Validator) and Feature 7 (Action Planner).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent.graph.state import InvestigationState
from apps.api.domain.context import TenantContext
from apps.api.models.tables import RunModel
from apps.api.services.run_service import RunService


@dataclass(frozen=True)
class HandoffResult:
    """Result of proposing a handoff for an investigated run."""

    target_status: str
    reason: str
    run_id: UUID
    transitioned: bool = True
    error: str | None = None
    details: dict[str, Any] | None = None


class HandoffPort(ABC):
    """Abstract port for handing off a completed investigation to downstream planning/policy services."""

    @abstractmethod
    def propose(
        self,
        ctx: TenantContext,
        run_id: UUID,
        state: InvestigationState,
        expected_version: int | None = None,
    ) -> HandoffResult:
        """Proposes handoff for a concluded investigation.

        Args:
            ctx: Tenant boundary context.
            run_id: Identifier of the run.
            state: InvestigationState containing draft diagnosis and audit evidence.
            expected_version: Optional expected run version for optimistic concurrency control.
        """
        ...


class DefaultHandoffStub(HandoffPort):
    """Default stub implementation of HandoffPort for Feature 5.

    Since downstream action planning and policy validation are built in Feature 6/7,
    the default stub marks the run as 'escalated' with reason
    'investigation_complete_no_planner'. This ensures every investigated run reaches a
    defined, audited terminal state under optimistic locking.
    """

    def __init__(self, run_service: RunService) -> None:
        self.run_service = run_service

    def propose(
        self,
        ctx: TenantContext,
        run_id: UUID,
        state: InvestigationState,
        expected_version: int | None = None,
    ) -> HandoffResult:
        if expected_version is None:
            current_run: RunModel = self.run_service.run_repo.get_by_id(ctx, run_id)
            version = current_run.version
        else:
            version = expected_version

        reason = "investigation_complete_no_planner"
        updated_run, event = self.run_service.transition(
            ctx=ctx,
            run_id=run_id,
            to_status="escalated",
            actor="investigator_handoff",
            expected_version=version,
            payload={
                "reason": reason,
                "notes": "Handoff stub reached terminal state; planner not configured.",
                "draft_diagnosis": state.get("draft_diagnosis"),
            },
        )
        return HandoffResult(
            target_status=updated_run.status,
            reason=reason,
            run_id=run_id,
            transitioned=True,
            details={"version": updated_run.version, "event_seq": event.seq},
        )
