"""agent/graph/nodes/await_review.py

LangGraph `await_review` interrupt node and `await_execution` stub for Feature 8.

Invariants for `await_review_node` (every statement before `interrupt()`):
1. `handoff = state.get("handoff_result") or {}`: Pure in-memory dict read from checkpointed state.
2. `plan_id = state.get("proposed_plan_id") or handoff.get("plan_id")`: Pure scalar extraction.
3. `plan_hash_val = state.get("proposed_plan_hash") or handoff.get("plan_hash")`: Pure scalar extraction.
4. `expires_at = state.get("proposed_expires_at") or handoff.get("expires_at")`: Pure scalar extraction.
5. `if plan_reader is not None and plan_id is not None: _ = plan_reader(state.get("tenant_id"), plan_id)`:
   Strictly read-only SELECT of the plan row and its current status (zero DB writes, zero
   approvals inserted, zero state transitions).
Because statements 1-5 perform zero mutations, LangGraph's re-execution of `await_review_node`
from the top of the node upon `Command(resume=...)` is 100% idempotent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from langgraph.types import interrupt

from agent.graph.state import InvestigationState


@runtime_checkable
class ExecutionPort(Protocol):
    """Port for handing off an approved plan to the Execution Broker via the DB queue."""

    def handoff_approved_plan(
        self,
        *,
        run_id: str,
        plan_id: str,
        action_execution_id: str | None,
    ) -> dict[str, Any]:
        """Verifies/hands off an approved plan via the DB-backed execution queue."""
        ...


class DatabaseExecutionPort:
    """DB-only ExecutionPort: confirms the queued action_execution row exists; never calls broker."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def handoff_approved_plan(
        self,
        *,
        run_id: str,
        plan_id: str,
        action_execution_id: str | None,
    ) -> dict[str, Any]:
        from uuid import UUID

        from sqlalchemy import select

        from apps.api.models.tables import ActionExecutionModel

        plan_uuid = UUID(str(plan_id))
        stmt = select(ActionExecutionModel).where(ActionExecutionModel.plan_id == plan_uuid)
        if action_execution_id:
            stmt = stmt.where(ActionExecutionModel.id == UUID(str(action_execution_id)))
        exec_row = self.session.scalar(stmt)
        if exec_row is None:
            return {
                "handoff_mode": "db_queue",
                "run_id": str(run_id),
                "plan_id": str(plan_id),
                "action_execution_id": action_execution_id,
                "status": "missing",
            }
        return {
            "handoff_mode": "db_queue",
            "run_id": str(run_id),
            "plan_id": str(plan_id),
            "action_execution_id": str(exec_row.id),
            "status": exec_row.status,
        }


def await_review_node(
    state: InvestigationState,
    *,
    plan_reader: Callable[[str | None, str | None], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Pauses graph execution at `await_review` until a DB decision is recorded."""
    # Statement 1-4: pure, side-effect-free reads from checkpointed state
    handoff = state.get("handoff_result") or {}
    plan_id = state.get("proposed_plan_id") or handoff.get("plan_id")
    plan_hash_val = state.get("proposed_plan_hash") or handoff.get("plan_hash")
    expires_at = state.get("proposed_expires_at") or handoff.get("expires_at")

    # Statement 5: optional read-only plan status check (never writes approvals or mutates state)
    if plan_reader is not None and plan_id is not None:
        _ = plan_reader(state.get("tenant_id"), str(plan_id))

    # Small, secret-free interrupt payload
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "plan_hash": plan_hash_val,
        "expires_at": expires_at,
    }

    resume_value = interrupt(payload)
    decision_data = resume_value if isinstance(resume_value, dict) else {"outcome": str(resume_value)}
    outcome = str(decision_data.get("outcome") or decision_data.get("decision") or "").lower()

    if outcome in ("approve", "approved"):
        return {
            "status": "approved",
            "approval_decision": "approved",
            "action_execution_id": decision_data.get("action_execution_id"),
        }

    if outcome in ("reject", "rejected"):
        return {
            "status": "escalated",
            "approval_decision": "rejected",
        }

    if outcome in ("expire", "expired"):
        return {
            "status": "escalated",
            "approval_decision": "expired",
        }

    if outcome in ("cancel", "cancelled"):
        return {
            "status": "escalated",
            "approval_decision": "cancelled",
        }

    return {
        "status": "escalated",
        "approval_decision": outcome or "unknown",
    }


def await_execution_node(
    state: InvestigationState,
    *,
    execution_port: ExecutionPort | None = None,
) -> dict[str, Any]:
    """Ends the worker graph cleanly after approval; broker drives execution via the DB queue."""
    if execution_port is not None:
        execution_port.handoff_approved_plan(
            run_id=str(state.get("run_id", "")),
            plan_id=str(state.get("proposed_plan_id", "")),
            action_execution_id=state.get("action_execution_id"),
        )
    return {"status": state.get("status", "approved")}
