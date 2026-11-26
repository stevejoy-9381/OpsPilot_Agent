"""Isolated Execution Broker worker loop, dispatch path, and /healthz server."""

from __future__ import annotations

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from apps.api.domain.context import TenantContext
from apps.api.domain.errors import LeaseLostError
from apps.api.logging import get_logger, setup_logging
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    ExecutionEventModel,
    RunModel,
)
from apps.api.repositories.execution_repository import ExecutionRepository
from apps.api.services.run_service import RunService
from broker.checks import Allowed, Refused, run_prechecks
from broker.reconcile import Reconciled, StillUnknown, reconcile
from broker.settings import BrokerSettings
from broker.target_client import (
    LabAdminClient,
    TargetActionOutcome,
    TargetActionResult,
    TargetClient,
)
from shared.targets import TargetRegistry, TargetState, load_target_registry

logger = get_logger("opspilot.broker")


class VerificationPort(Protocol):
    """Hand-off port from Broker execution acknowledgment to Feature 10 Verification."""

    def handoff(
        self,
        session: Session,
        ctx: TenantContext,
        run: RunModel,
        execution: ActionExecutionModel,
        plan: ActionPlanModel,
    ) -> None:
        ...


class DefaultVerificationPort:
    """Default VerificationPort: transitions run executing -> verifying via RunService and stops."""

    def handoff(
        self,
        session: Session,
        ctx: TenantContext,
        run: RunModel,
        execution: ActionExecutionModel,
        plan: ActionPlanModel,
    ) -> None:
        RunService(session).transition(
            ctx=ctx,
            run_id=run.id,
            to_status="verifying",
            actor="broker",
            expected_version=run.version,
            payload={
                "execution_id": str(execution.id),
                "plan_id": str(plan.id),
            },
        )


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal read-only /healthz endpoint; rejects all other paths."""

    session_factory: sessionmaker[Session] | None = None

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        ok = False
        if self.session_factory is not None:
            try:
                with self.session_factory() as s:
                    s.execute(text("SELECT 1"))
                ok = True
            except Exception:
                ok = False
        body = json.dumps({"status": "ok" if ok else "unavailable"}).encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def start_healthz_server(
    host: str, port: int, session_factory: sessionmaker[Session]
) -> ThreadingHTTPServer:
    """Starts the internal /healthz HTTP server in a daemon thread."""

    class BoundHandler(_HealthHandler):
        pass

    BoundHandler.session_factory = session_factory
    server = ThreadingHTTPServer((host, port), BoundHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class BrokerService:
    """Broker worker loop claiming queued executions, running B1-B10, and dispatching."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        target_client: TargetClient | None,
        target_registry: TargetRegistry | Path | None = None,
        verification_port: VerificationPort | None = None,
        broker_id: str = "broker-1",
        lease_seconds: int = 30,
        heartbeat_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        reconcile_deadline_seconds: float = 15.0,
    ) -> None:
        self.session_factory = session_factory
        self.target_client = target_client
        self.target_registry = target_registry
        self.verification_port: VerificationPort = verification_port or DefaultVerificationPort()
        self.broker_id = broker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.reconcile_deadline_seconds = reconcile_deadline_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signals the broker loop to stop claiming new executions."""
        self._stop_event.set()

    def _release_lease(self, execution_id: UUID, lease_token: UUID) -> None:
        """Releases lease on graceful shutdown if still held by this broker."""
        try:
            with self.session_factory() as session:
                session.execute(
                    text(
                        """
                        UPDATE app.action_executions
                        SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                        WHERE id = :eid AND lease_token = :tok
                        """
                    ),
                    {"eid": execution_id, "tok": lease_token},
                )
                session.commit()
        except Exception:
            logger.warning("Failed releasing broker lease", execution_id=str(execution_id))

    def _handle_refusal(
        self,
        session: Session,
        exec_repo: ExecutionRepository,
        execution: ActionExecutionModel,
        lease_token: UUID,
        refusal: Refused,
    ) -> None:
        """Marks execution refused, updates plan status, and escalates run via RunService."""
        reason = f"execution_refused:{refusal.code}"
        error_payload = {
            "code": refusal.code,
            "reason": reason,
            "message": refusal.detail,
        }
        exec_repo.mark_refused(
            execution_id=execution.id,
            lease_token=lease_token,
            refusal_code=refusal.code,
            last_error=error_payload,
        )

        plan_status = "failed"
        if refusal.code == "execution_expired":
            plan_status = "expired"
        elif refusal.code == "stale_target":
            plan_status = "superseded"

        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(execution.tenant_id)},
        )
        session.execute(
            update(ActionPlanModel)
            .where(
                ActionPlanModel.id == execution.plan_id,
                ActionPlanModel.tenant_id == execution.tenant_id,
            )
            .values(status=plan_status)
        )

        plan = session.scalar(
            select(ActionPlanModel).where(
                ActionPlanModel.id == execution.plan_id,
                ActionPlanModel.tenant_id == execution.tenant_id,
            )
        )
        if plan is not None:
            run = session.scalar(
                select(RunModel).where(
                    RunModel.id == plan.run_id,
                    RunModel.tenant_id == execution.tenant_id,
                )
            )
            if run is not None and run.status == "approved":
                ctx = TenantContext(tenant_id=execution.tenant_id, role="broker")
                run_svc = RunService(session)
                run_svc.transition(
                    ctx=ctx,
                    run_id=run.id,
                    to_status="escalated",
                    actor="broker",
                    expected_version=run.version,
                    payload={"reason": reason, "refusal_code": refusal.code},
                )
                run_svc.record_error(
                    ctx=ctx,
                    run_id=run.id,
                    error=error_payload,
                    actor="broker",
                )

        logger.info(
            "Execution refused",
            execution_id=str(execution.id),
            refusal_code=refusal.code,
        )

    def _start_heartbeat(
        self, execution_id: UUID, lease_token: UUID, done_event: threading.Event
    ) -> threading.Thread:
        def _beat() -> None:
            while not done_event.wait(self.heartbeat_seconds):
                if self._stop_event.is_set():
                    break
                try:
                    with self.session_factory() as hb_session:
                        ExecutionRepository(hb_session).heartbeat(
                            execution_id, lease_token, self.lease_seconds
                        )
                        hb_session.commit()
                except LeaseLostError:
                    break
                except Exception:
                    break

        t = threading.Thread(target=_beat, daemon=True)
        t.start()
        return t

    def _call_target(
        self,
        *,
        target_id: str,
        service: str,
        action: str,
        action_key: str,
        expected_version: str,
        rollback_to_version: str | None,
    ) -> TargetActionResult:
        assert self.target_client is not None
        try:
            if action == "rollback_service":
                assert rollback_to_version is not None
                return self.target_client.apply_rollback(
                    target_id=target_id,
                    action_key=action_key,
                    expected_current_version=expected_version,
                    to_version=rollback_to_version,
                )
            return self.target_client.apply_restart(
                target_id=target_id,
                action_key=action_key,
                expected_current_version=expected_version,
            )
        except (httpx.TimeoutException, httpx.TransportError, ConnectionResetError, OSError) as exc:
            return TargetActionResult(
                outcome=TargetActionOutcome.UNCERTAIN,
                service=service,
                action=action,
                detail=f"Transport failure: {type(exc).__name__}",
            )

    def _assert_reconciled_not_applied(self, session: Session, execution_id: UUID) -> None:
        """Guard: forbids re-dispatching from uncertain without a prior not_applied reconcile record."""
        events = session.scalars(
            select(ExecutionEventModel)
            .where(ExecutionEventModel.execution_id == execution_id)
            .order_by(ExecutionEventModel.seq.asc())
        ).all()
        last_uncertain_seq = max(
            (
                e.seq
                for e in events
                if e.type in ("execution_uncertain", "execution_reclaimed_uncertain")
            ),
            default=0,
        )
        has_not_applied = any(
            e.seq > last_uncertain_seq
            and e.type == "reconcile_result"
            and e.payload.get("classification") == "not_applied"
            for e in events
        )
        if not has_not_applied:
            raise RuntimeError(
                f"Guard violation: attempted re-dispatch for uncertain execution {execution_id} "
                "without prior reconcile_result(not_applied)"
            )

    def _map_target_response(
        self,
        *,
        session: Session,
        exec_repo: ExecutionRepository,
        execution_id: UUID,
        lease_token: UUID,
        tenant_id: UUID,
        run_id: UUID,
        plan_id: UUID,
        target_id: str,
        action_result: TargetActionResult,
    ) -> str:
        ctx = TenantContext(tenant_id=tenant_id, role="broker")
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        run_row = session.scalar(
            select(RunModel).where(RunModel.id == run_id, RunModel.tenant_id == tenant_id)
        )
        plan_row = session.scalar(
            select(ActionPlanModel).where(
                ActionPlanModel.id == plan_id, ActionPlanModel.tenant_id == tenant_id
            )
        )
        assert run_row is not None and plan_row is not None
        redacted_resp = action_result.to_redacted_dict()

        if action_result.outcome in (
            TargetActionOutcome.APPLIED,
            TargetActionOutcome.ALREADY_APPLIED,
        ):
            after_state = (
                self.target_client.get_state(target_id)
                if self.target_client is not None
                else None
            )
            after_dict = (
                after_state.model_dump(mode="json")
                if isinstance(after_state, TargetState)
                else None
            )
            ex_ack = exec_repo.mark_acknowledged(
                execution_id=execution_id,
                lease_token=lease_token,
                target_response=redacted_resp,
                target_state_after=after_dict,
            )
            if action_result.outcome == TargetActionOutcome.ALREADY_APPLIED:
                exec_repo.append_custom_event(
                    execution_id=execution_id,
                    tenant_id=tenant_id,
                    event_type="already_applied",
                    payload=redacted_resp,
                )
            session.execute(
                update(ActionPlanModel)
                .where(ActionPlanModel.id == plan_id, ActionPlanModel.tenant_id == tenant_id)
                .values(status="executed")
            )
            self.verification_port.handoff(
                session=session, ctx=ctx, run=run_row, execution=ex_ack, plan=plan_row
            )
            return action_result.outcome.value

        if action_result.outcome == TargetActionOutcome.PRECONDITION_FAILED:
            err = {
                "code": "target_changed",
                "message": action_result.detail or "Target version changed before apply",
            }
            exec_repo.mark_failed(
                execution_id=execution_id,
                lease_token=lease_token,
                last_error=err,
                target_response=redacted_resp,
            )
            session.execute(
                update(ActionPlanModel)
                .where(ActionPlanModel.id == plan_id, ActionPlanModel.tenant_id == tenant_id)
                .values(status="superseded")
            )
            run_svc = RunService(session)
            run_svc.transition(
                ctx=ctx,
                run_id=run_row.id,
                to_status="escalated",
                actor="broker",
                expected_version=run_row.version,
                payload={"reason": "target_changed"},
            )
            run_svc.record_error(ctx=ctx, run_id=run_row.id, error=err, actor="broker")
            return "precondition_failed"

        if action_result.outcome == TargetActionOutcome.CLIENT_ERROR:
            err = {
                "code": "target_client_error",
                "message": action_result.detail or "Target rejected request (4xx)",
            }
            exec_repo.mark_failed(
                execution_id=execution_id,
                lease_token=lease_token,
                last_error=err,
                target_response=redacted_resp,
            )
            session.execute(
                update(ActionPlanModel)
                .where(ActionPlanModel.id == plan_id, ActionPlanModel.tenant_id == tenant_id)
                .values(status="failed")
            )
            run_svc = RunService(session)
            run_svc.transition(
                ctx=ctx,
                run_id=run_row.id,
                to_status="escalated",
                actor="broker",
                expected_version=run_row.version,
                payload={"reason": "target_client_error"},
            )
            run_svc.record_error(ctx=ctx, run_id=run_row.id, error=err, actor="broker")
            return "failed"

        exec_repo.mark_uncertain(
            execution_id=execution_id,
            lease_token=lease_token,
            last_error={
                "code": "execution_uncertain",
                "message": action_result.detail or "Uncertain target outcome",
            },
            target_response=redacted_resp,
        )
        return "uncertain"

    def reconcile_execution(self, execution_id: UUID, lease_token: UUID) -> str:
        """Reconciles an uncertain execution before any retry or final escalation."""
        deadline = time.monotonic() + self.reconcile_deadline_seconds

        with self.session_factory() as session:
            exec_repo = ExecutionRepository(session)
            ex = session.get(ActionExecutionModel, execution_id)
            assert ex is not None
            tenant_id = ex.tenant_id
            plan_id = ex.plan_id
            action_key = ex.action_key
            session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            plan = session.get(ActionPlanModel, plan_id)
            assert plan is not None
            run_id = plan.run_id
            target_id = ex.target_id or plan.target_id or ""
            service_name = plan.service or ""
            plan_action = plan.action
            expected_version = plan.expected_version or ""
            rollback_to_version = plan.rollback_to_version
            exec_repo.append_custom_event(
                execution_id, tenant_id, "reconcile_started", {"target_id": target_id}
            )
            session.commit()

        last_unknown = StillUnknown(reason="reconcile_deadline_exceeded", detail="Deadline reached")
        while True:
            with self.session_factory() as session:
                exec_repo = ExecutionRepository(session)
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
                ex_row = session.get(ActionExecutionModel, execution_id)
                pl_row = session.get(ActionPlanModel, plan_id)
                assert ex_row is not None and pl_row is not None
                rec = reconcile(ex_row, pl_row, target_id, self.target_client)

                if isinstance(rec, Reconciled):
                    exec_repo.append_custom_event(
                        execution_id,
                        tenant_id,
                        "reconcile_result",
                        {"classification": rec.outcome, "detail": rec.detail},
                    )
                    if rec.outcome == "applied":
                        ex_rec = exec_repo.mark_reconciled(
                            execution_id=execution_id,
                            lease_token=lease_token,
                            target_response=rec.target_response,
                            target_state_after=rec.target_state_after,
                        )
                        exec_repo.append_custom_event(
                            execution_id,
                            tenant_id,
                            "reconciled_applied",
                            rec.target_response or {},
                        )
                        session.execute(
                            update(ActionPlanModel)
                            .where(
                                ActionPlanModel.id == plan_id,
                                ActionPlanModel.tenant_id == tenant_id,
                            )
                            .values(status="executed")
                        )
                        run_row = session.get(RunModel, run_id)
                        assert run_row is not None
                        ctx = TenantContext(tenant_id=tenant_id, role="broker")
                        if run_row.status == "approved":
                            run_row, _ = RunService(session).transition(
                                ctx=ctx,
                                run_id=run_id,
                                to_status="executing",
                                actor="broker",
                                expected_version=run_row.version,
                                payload={"execution_id": str(execution_id)},
                            )
                        self.verification_port.handoff(
                            session=session,
                            ctx=ctx,
                            run=run_row,
                            execution=ex_rec,
                            plan=pl_row,
                        )
                        session.commit()
                        return "reconciled_applied"

                    # rec.outcome == "not_applied"
                    if ex_row.attempts >= self.max_attempts:
                        err = {
                            "code": "execution_retries_exhausted",
                            "message": f"Exhausted max_attempts={self.max_attempts}",
                        }
                        exec_repo.mark_failed(
                            execution_id=execution_id, lease_token=lease_token, last_error=err
                        )
                        session.execute(
                            update(ActionPlanModel)
                            .where(
                                ActionPlanModel.id == plan_id,
                                ActionPlanModel.tenant_id == tenant_id,
                            )
                            .values(status="failed")
                        )
                        run_row = session.get(RunModel, run_id)
                        if run_row is not None and run_row.status in ("approved", "executing"):
                            ctx = TenantContext(tenant_id=tenant_id, role="broker")
                            run_svc = RunService(session)
                            run_svc.transition(
                                ctx=ctx,
                                run_id=run_id,
                                to_status="escalated",
                                actor="broker",
                                expected_version=run_row.version,
                                payload={"reason": "execution_retries_exhausted"},
                            )
                            run_svc.record_error(ctx=ctx, run_id=run_id, error=err, actor="broker")
                        session.commit()
                        return "failed"

                    exec_repo.append_custom_event(
                        execution_id,
                        tenant_id,
                        "retry_scheduled",
                        {"attempt": ex_row.attempts + 1, "max_attempts": self.max_attempts},
                    )
                    session.commit()

                    backoff = min(
                        self.retry_backoff_seconds * max(1, ex_row.attempts),
                        max(0.0, deadline - time.monotonic()),
                    )
                    if backoff > 0:
                        time.sleep(backoff)

                    with self.session_factory() as retry_sess:
                        self._assert_reconciled_not_applied(retry_sess, execution_id)
                        retry_repo = ExecutionRepository(retry_sess)
                        retry_repo.mark_dispatched(
                            execution_id=execution_id,
                            lease_token=lease_token,
                            target_state_before=rec.target_state_before,
                            target_id=target_id,
                        )
                        retry_sess.commit()

                    action_result = self._call_target(
                        target_id=target_id,
                        service=service_name,
                        action=plan_action,
                        action_key=action_key,
                        expected_version=expected_version,
                        rollback_to_version=rollback_to_version,
                    )
                    with self.session_factory() as map_sess:
                        outcome = self._map_target_response(
                            session=map_sess,
                            exec_repo=ExecutionRepository(map_sess),
                            execution_id=execution_id,
                            lease_token=lease_token,
                            tenant_id=tenant_id,
                            run_id=run_id,
                            plan_id=plan_id,
                            target_id=target_id,
                            action_result=action_result,
                        )
                        map_sess.commit()
                    if outcome != "uncertain":
                        return outcome
                    continue

                # StillUnknown
                last_unknown = rec
                exec_repo.append_custom_event(
                    execution_id,
                    tenant_id,
                    "reconcile_result",
                    {
                        "classification": "still_unknown",
                        "reason": rec.reason,
                        "detail": rec.detail,
                    },
                )
                if time.monotonic() >= deadline or self._stop_event.is_set():
                    err = {"code": "outcome_unknown", "message": last_unknown.detail}
                    exec_repo.append_custom_event(
                        execution_id, tenant_id, "escalated_unknown", err
                    )
                    exec_repo.mark_failed(
                        execution_id=execution_id, lease_token=lease_token, last_error=err
                    )
                    session.execute(
                        update(ActionPlanModel)
                        .where(
                            ActionPlanModel.id == plan_id,
                            ActionPlanModel.tenant_id == tenant_id,
                        )
                        .values(status="failed")
                    )
                    run_row = session.get(RunModel, run_id)
                    if run_row is not None and run_row.status in ("approved", "executing"):
                        ctx = TenantContext(tenant_id=tenant_id, role="broker")
                        run_svc = RunService(session)
                        run_svc.transition(
                            ctx=ctx,
                            run_id=run_id,
                            to_status="escalated",
                            actor="broker",
                            expected_version=run_row.version,
                            payload={"reason": "outcome_unknown"},
                        )
                        run_svc.record_error(ctx=ctx, run_id=run_id, error=err, actor="broker")
                    session.commit()
                    return "escalated_unknown"

                session.commit()
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def process_next(self) -> str | None:
        """Claims one execution, runs B1-B10, dispatches once, and maps the result."""
        if self._stop_event.is_set():
            return None

        with self.session_factory() as session:
            exec_repo = ExecutionRepository(session)
            claimed = exec_repo.claim_next(
                lease_owner=self.broker_id, lease_seconds=self.lease_seconds
            )
            if claimed is None:
                session.commit()
                return None
            session.commit()
            execution_id = claimed.id
            lease_token = claimed.lease_token
            claimed_status = claimed.status
            assert lease_token is not None

        hb_done = threading.Event()
        hb_thread = self._start_heartbeat(execution_id, lease_token, hb_done)
        try:
            if self._stop_event.is_set():
                self._release_lease(execution_id, lease_token)
                return None

            # Idempotent resume: reclaimed dispatched/uncertain rows reconcile BEFORE any re-dispatch
            if claimed_status == "uncertain":
                return self.reconcile_execution(execution_id, lease_token)

            # Phase 1: B1-B10 + commit 'dispatched' BEFORE calling target
            with self.session_factory() as session:
                exec_repo = ExecutionRepository(session)
                now_from_db = session.scalar(select(func.clock_timestamp()))
                assert now_from_db is not None
                result = run_prechecks(
                    session=session,
                    execution_id=execution_id,
                    now_from_db=now_from_db,
                    target_reader=self.target_client,
                    target_registry=self.target_registry,
                )

                if isinstance(result, Refused):
                    execution = session.scalar(
                        select(ActionExecutionModel).where(ActionExecutionModel.id == execution_id)
                    )
                    if execution is not None:
                        self._handle_refusal(session, exec_repo, execution, lease_token, result)
                    session.commit()
                    return "refused"

                assert isinstance(result, Allowed)
                allowed = result
                tenant_id = allowed.execution.tenant_id
                run_id = allowed.run.id
                plan_id = allowed.plan.id
                target_id = allowed.target_spec.target_id
                service_name = allowed.target_spec.service
                plan_action = allowed.plan.action
                action_key = allowed.execution.action_key
                expected_version = allowed.plan.expected_version or ""
                rollback_to_version = allowed.plan.rollback_to_version

                ctx = TenantContext(tenant_id=tenant_id, role="broker")
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
                RunService(session).transition(
                    ctx=ctx,
                    run_id=run_id,
                    to_status="executing",
                    actor="broker",
                    expected_version=allowed.run.version,
                    payload={"execution_id": str(execution_id)},
                )
                exec_repo.mark_dispatched(
                    execution_id=execution_id,
                    lease_token=lease_token,
                    target_state_before=allowed.target_state.model_dump(mode="json"),
                    target_id=target_id,
                )
                session.commit()

            # Phase 2: Call target outside DB transaction
            action_result = self._call_target(
                target_id=target_id,
                service=service_name,
                action=plan_action,
                action_key=action_key,
                expected_version=expected_version,
                rollback_to_version=rollback_to_version,
            )

            # Phase 3: Map target response in a new DB transaction
            with self.session_factory() as session:
                outcome = self._map_target_response(
                    session=session,
                    exec_repo=ExecutionRepository(session),
                    execution_id=execution_id,
                    lease_token=lease_token,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    plan_id=plan_id,
                    target_id=target_id,
                    action_result=action_result,
                )
                session.commit()
                return outcome
        finally:
            hb_done.set()
            hb_thread.join(timeout=1.0)

    def run_forever(self) -> None:
        """Main polling loop until stop() is signaled."""
        while not self._stop_event.is_set():
            outcome = self.process_next()
            if outcome is None:
                self._stop_event.wait(self.poll_interval_seconds)


def main() -> None:
    """Entrypoint for the isolated Execution Broker process."""
    settings = BrokerSettings()  # type: ignore[call-arg]
    setup_logging(settings.log_level)

    db_url = settings.database_url.get_secret_value()
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+psycopg://" + db_url[len("postgresql://") :]

    engine = create_engine(db_url, future=True)
    session_factory = sessionmaker(bind=engine, future=True)
    registry = load_target_registry(settings.lab_targets_path)
    target_client = LabAdminClient(registry=registry, admin_token=settings.lab_admin_token)
    health_server = start_healthz_server(
        settings.healthz_host, settings.healthz_port, session_factory
    )

    service = BrokerService(
        session_factory=session_factory,
        target_client=target_client,
        target_registry=registry,
        broker_id=settings.broker_id,
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
    )

    def _on_signal(signum: int, _frame: Any) -> None:
        logger.info("Broker received shutdown signal", signal=signum)
        service.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        service.run_forever()
    finally:
        health_server.shutdown()
        engine.dispose()


if __name__ == "__main__":
    main()
