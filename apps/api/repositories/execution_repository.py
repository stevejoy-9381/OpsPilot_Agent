"""Repository for app.action_executions and append-only app.execution_events.

Enforces:
1. Single-row atomic claim via FOR UPDATE SKIP LOCKED across queued and lease-expired rows.
2. Mandatory lease_token fencing on every write (raises LeaseLostError on mismatch).
3. Tenant isolation invariant: tenant_id is always read from the execution row, never caller input.
4. Gap-free monotonic seq numbering on app.execution_events in the same transaction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from apps.api.domain.errors import LeaseLostError, NotFoundError
from apps.api.models.enums import ExecutionStatus
from apps.api.models.tables import ActionExecutionModel, ExecutionEventModel


class ExecutionRepository:
    """Repository handling Broker execution claims, fenced state transitions, and events."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _append_event(
        self,
        execution_id: UUID,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        actor: str = "broker",
    ) -> ExecutionEventModel:
        """Appends an execution_event with the next gap-free seq for execution_id."""
        next_seq = self.session.scalar(
            select(func.coalesce(func.max(ExecutionEventModel.seq), 0) + 1).where(
                ExecutionEventModel.execution_id == execution_id
            )
        )
        event = ExecutionEventModel(
            execution_id=execution_id,
            tenant_id=tenant_id,
            seq=int(next_seq or 1),
            type=event_type,
            actor=actor,
            payload=payload or {},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def claim_next(
        self,
        lease_owner: str,
        lease_seconds: int = 30,
    ) -> ActionExecutionModel | None:
        """Atomically claims the next queued or lease-expired execution using SKIP LOCKED.

        - queued -> claimed
        - expired claimed -> claimed (new lease_token)
        - expired dispatched -> uncertain (crash after dispatch; requires reconcile)
        - expired uncertain -> uncertain (new lease_token)
        """
        row = self.session.execute(
            text(
                """
                SELECT id, tenant_id, status
                FROM app.action_executions
                WHERE status = 'queued'
                   OR (
                       status IN ('claimed', 'dispatched', 'uncertain')
                       AND lease_expires_at < clock_timestamp()
                   )
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
        ).fetchone()

        if row is None:
            return None

        exec_id: UUID = row[0]
        tenant_id: UUID = row[1]
        prior_status: str = row[2]
        new_token = uuid4()

        if prior_status == ExecutionStatus.QUEUED.value:
            next_status = ExecutionStatus.CLAIMED.value
            event_type = "execution_claimed"
        elif prior_status == ExecutionStatus.DISPATCHED.value:
            next_status = ExecutionStatus.UNCERTAIN.value
            event_type = "execution_reclaimed_uncertain"
        else:
            next_status = prior_status
            event_type = "execution_reclaimed"

        updated = self.session.execute(
            text(
                """
                UPDATE app.action_executions
                SET status = :next_status,
                    lease_owner = :lease_owner,
                    lease_token = :lease_token,
                    lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
                    heartbeat_at = clock_timestamp()
                WHERE id = :exec_id
                RETURNING id
                """
            ),
            {
                "next_status": next_status,
                "lease_owner": lease_owner,
                "lease_token": new_token,
                "lease_seconds": lease_seconds,
                "exec_id": exec_id,
            },
        ).fetchone()

        if updated is None:
            return None

        self._append_event(
            execution_id=exec_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload={
                "prior_status": prior_status,
                "new_status": next_status,
                "lease_owner": lease_owner,
                "lease_token": str(new_token),
            },
        )

        model = self.session.get(ActionExecutionModel, exec_id)
        if model is not None:
            self.session.refresh(model)
        return model

    def _fenced_transition(
        self,
        execution_id: UUID,
        lease_token: UUID,
        to_status: ExecutionStatus,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        extra_values: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> ActionExecutionModel:
        """Performs a lease_token-fenced UPDATE on app.action_executions + appends event."""
        values: dict[str, Any] = {
            "status": to_status.value,
            "heartbeat_at": func.clock_timestamp(),
        }
        if increment_attempts:
            values["attempts"] = ActionExecutionModel.attempts + 1
        if extra_values:
            values.update(extra_values)

        stmt = (
            update(ActionExecutionModel)
            .where(
                ActionExecutionModel.id == execution_id,
                ActionExecutionModel.lease_token == lease_token,
            )
            .values(**values)
            .returning(ActionExecutionModel.id, ActionExecutionModel.tenant_id)
        )
        row = self.session.execute(stmt).fetchone()
        if row is None:
            exists = self.session.scalar(
                select(ActionExecutionModel.id).where(ActionExecutionModel.id == execution_id)
            )
            if exists is None:
                raise NotFoundError(f"Execution {execution_id} not found")
            raise LeaseLostError(
                f"Broker lease lost for execution {execution_id} (fencing token mismatch)",
                details={"execution_id": str(execution_id), "lease_token": str(lease_token)},
            )

        tenant_id: UUID = row[1]
        self._append_event(
            execution_id=execution_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=event_payload or {"status": to_status.value},
        )

        model = self.session.get(ActionExecutionModel, execution_id)
        assert model is not None
        self.session.refresh(model)
        return model

    def heartbeat(
        self,
        execution_id: UUID,
        lease_token: UUID,
        lease_seconds: int = 30,
    ) -> None:
        """Refreshes heartbeat_at and lease_expires_at if lease_token still matches."""
        row = self.session.execute(
            text(
                """
                UPDATE app.action_executions
                SET heartbeat_at = clock_timestamp(),
                    lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds)
                WHERE id = :exec_id
                  AND lease_token = :lease_token
                RETURNING id
                """
            ),
            {
                "exec_id": execution_id,
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
            },
        ).fetchone()
        if row is None:
            raise LeaseLostError(
                f"Broker lease lost during heartbeat for execution {execution_id}",
                details={"execution_id": str(execution_id), "lease_token": str(lease_token)},
            )

    def mark_dispatched(
        self,
        execution_id: UUID,
        lease_token: UUID,
        target_state_before: dict[str, Any] | None = None,
        target_id: str | None = None,
    ) -> ActionExecutionModel:
        """Transitions claimed|uncertain -> dispatched, incrementing attempts."""
        extra: dict[str, Any] = {"dispatched_at": func.clock_timestamp()}
        if target_state_before is not None:
            extra["target_state_before"] = target_state_before
        if target_id is not None:
            extra["target_id"] = target_id
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.DISPATCHED,
            event_type="execution_dispatched",
            event_payload={"target_state_before": target_state_before},
            extra_values=extra,
            increment_attempts=True,
        )

    def append_custom_event(
        self,
        execution_id: UUID,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> ExecutionEventModel:
        """Appends a custom execution_event in the current transaction."""
        return self._append_event(
            execution_id=execution_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
        )

    def mark_acknowledged(
        self,
        execution_id: UUID,
        lease_token: UUID,
        target_response: dict[str, Any] | None = None,
        target_state_after: dict[str, Any] | None = None,
    ) -> ActionExecutionModel:
        """Transitions dispatched -> acknowledged."""
        extra: dict[str, Any] = {
            "acknowledged_at": func.clock_timestamp(),
            "completed_at": func.clock_timestamp(),
        }
        if target_response is not None:
            extra["target_response"] = target_response
        if target_state_after is not None:
            extra["target_state_after"] = target_state_after
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.ACKNOWLEDGED,
            event_type="execution_acknowledged",
            event_payload={"target_response": target_response},
            extra_values=extra,
        )

    def mark_uncertain(
        self,
        execution_id: UUID,
        lease_token: UUID,
        last_error: dict[str, Any] | None = None,
        target_response: dict[str, Any] | None = None,
    ) -> ActionExecutionModel:
        """Transitions dispatched -> uncertain."""
        extra: dict[str, Any] = {}
        if last_error is not None:
            extra["last_error"] = last_error
        if target_response is not None:
            extra["target_response"] = target_response
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.UNCERTAIN,
            event_type="execution_uncertain",
            event_payload={"last_error": last_error},
            extra_values=extra,
        )

    def mark_reconciled(
        self,
        execution_id: UUID,
        lease_token: UUID,
        target_response: dict[str, Any] | None = None,
        target_state_after: dict[str, Any] | None = None,
    ) -> ActionExecutionModel:
        """Transitions uncertain -> reconciled."""
        extra: dict[str, Any] = {
            "reconciled_at": func.clock_timestamp(),
            "completed_at": func.clock_timestamp(),
        }
        if target_response is not None:
            extra["target_response"] = target_response
        if target_state_after is not None:
            extra["target_state_after"] = target_state_after
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.RECONCILED,
            event_type="execution_reconciled",
            event_payload={"target_response": target_response},
            extra_values=extra,
        )

    def mark_succeeded(
        self,
        execution_id: UUID,
        lease_token: UUID,
    ) -> ActionExecutionModel:
        """Transitions acknowledged|reconciled -> succeeded."""
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.SUCCEEDED,
            event_type="execution_succeeded",
            event_payload={"status": ExecutionStatus.SUCCEEDED.value},
        )

    def mark_failed(
        self,
        execution_id: UUID,
        lease_token: UUID,
        last_error: dict[str, Any] | None = None,
        target_response: dict[str, Any] | None = None,
    ) -> ActionExecutionModel:
        """Transitions claimed|uncertain -> failed."""
        extra: dict[str, Any] = {"completed_at": func.clock_timestamp()}
        if last_error is not None:
            extra["last_error"] = last_error
        if target_response is not None:
            extra["target_response"] = target_response
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.FAILED,
            event_type="execution_failed",
            event_payload={"last_error": last_error},
            extra_values=extra,
        )

    def mark_refused(
        self,
        execution_id: UUID,
        lease_token: UUID,
        refusal_code: str,
        last_error: dict[str, Any] | None = None,
        target_response: dict[str, Any] | None = None,
    ) -> ActionExecutionModel:
        """Transitions claimed|dispatched -> refused."""
        extra: dict[str, Any] = {
            "refusal_code": refusal_code,
            "completed_at": func.clock_timestamp(),
        }
        if last_error is not None:
            extra["last_error"] = last_error
        if target_response is not None:
            extra["target_response"] = target_response
        return self._fenced_transition(
            execution_id=execution_id,
            lease_token=lease_token,
            to_status=ExecutionStatus.REFUSED,
            event_type="execution_refused",
            event_payload={"refusal_code": refusal_code, "last_error": last_error},
            extra_values=extra,
        )
