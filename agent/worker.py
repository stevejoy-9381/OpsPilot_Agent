"""agent/worker.py

Background worker daemon claiming and executing queued incident investigation runs.

Requirements:
1. Claims queued runs with `SELECT ... FOR UPDATE SKIP LOCKED`.
2. Transitions run `queued -> investigating` via RunService under optimistic concurrency control.
3. Authenticates as the service principal (`role="worker"`). Cannot create approvals.
4. Executes the LangGraph investigator graph under a hard wall-clock timeout (`RUN_TIMEOUT_SECONDS`).
5. Updates worker heartbeat and lease metadata (`heartbeat_at`, `worker_id`).
6. Concurrency limit enforced from environment (`WORKER_CONCURRENCY`).
7. Clean shutdown and deterministic single-step helper for end-to-end integration tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

from langgraph.types import Command
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub, HandoffPort
from agent.graph.state import InvestigationState, create_initial_state
from agent.llm.interface import LLMClient
from apps.api.auth.dependencies import get_db_session_factory
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import LeaseLostError
from apps.api.models.tables import (
    ActionExecutionModel,
    ActionPlanModel,
    IncidentModel,
    RunModel,
)
from apps.api.repositories.plan_repository import PlanRepository
from apps.api.services.run_service import FencedRunService, RunService
from tools.registry import ToolRegistry, registry

logger = logging.getLogger("opspilot.agent.worker")

DEFAULT_WORKER_CONCURRENCY: int = 1
DEFAULT_RUN_TIMEOUT_SECONDS: float = 120.0
DEFAULT_POLL_INTERVAL_SECONDS: float = 0.5
# Lease-to-heartbeat ratio = 6:1 (30s lease / 5s heartbeat) so up to 5 missed heartbeats
# are tolerated before reclaim without false-positive lease loss under transient GC/IO pauses.
DEFAULT_HEARTBEAT_SECONDS: float = 5.0
DEFAULT_LEASE_SECONDS: float = 30.0
DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS: float = 15.0


class WorkerMetrics:
    """In-process counters for Feature 8 lease reclaim and resume tracking."""

    def __init__(self) -> None:
        self.reclaimed_runs: int = 0
        self.resumes_after_kill: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "reclaimed_runs": self.reclaimed_runs,
            "resumes_after_kill": self.resumes_after_kill,
        }


worker_metrics = WorkerMetrics()


class WorkerService:
    """Worker daemon orchestrating run claiming, graph execution, and heartbeat updates."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        llm_client: LLMClient | None = None,
        tool_registry: ToolRegistry | None = None,
        handoff_port: HandoffPort | None = None,
        worker_id: str | None = None,
        concurrency: int | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        checkpointer_type: str = "memory",
        postgres_conn_string: str | None = None,
        lease_ttl_seconds: float | None = None,
    ) -> None:
        self.session_factory = session_factory or get_db_session_factory()
        self.llm_client = llm_client
        self.tool_registry = tool_registry or registry
        self.handoff_port = handoff_port
        self.worker_id = (
            worker_id
            or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        )
        self.concurrency = concurrency or int(
            os.getenv("WORKER_CONCURRENCY", str(DEFAULT_WORKER_CONCURRENCY))
        )
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("RUN_TIMEOUT_SECONDS", str(DEFAULT_RUN_TIMEOUT_SECONDS))
        )
        self.poll_interval_seconds = poll_interval_seconds or float(
            os.getenv("WORKER_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))
        )
        self.lease_ttl_seconds = lease_ttl_seconds or float(
            os.getenv("LEASE_SECONDS", os.getenv("WORKER_LEASE_TTL_SECONDS", str(DEFAULT_LEASE_SECONDS)))
        )
        self.heartbeat_seconds = float(
            os.getenv("HEARTBEAT_SECONDS", str(DEFAULT_HEARTBEAT_SECONDS))
        )
        self.expiry_sweep_interval_seconds = float(
            os.getenv("EXPIRY_SWEEP_INTERVAL_SECONDS", str(DEFAULT_EXPIRY_SWEEP_INTERVAL_SECONDS))
        )
        self.checkpointer_type = checkpointer_type or os.getenv("CHECKPOINTER_TYPE", "memory")
        self.postgres_conn_string = (
            postgres_conn_string
            or os.getenv("CHECKPOINTER_DATABASE_URL")
            or os.getenv("DATABASE_URL")
        )
        self._running = False

    @staticmethod
    def _db_now(session: Session) -> datetime:
        raw = session.scalar(select(func.clock_timestamp()))
        if isinstance(raw, datetime):
            return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
        if isinstance(raw, str):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def claim_next_run(self, session: Session) -> tuple[UUID, UUID, str, int, UUID] | None:
        """Atomically claims a claimable run using SELECT ... FOR UPDATE SKIP LOCKED.

        Claimable runs:
        - `queued`
        - `investigating` or `verifying` with an expired lease (`lease_expires_at < db_now`)
        - `approved` needing resume (`lease_expires_at IS NULL OR lease_expires_at < db_now`)

        Note: `needs_review` is never claimed until a decision exists, and no state ever
        reclaims into `executing` (execution is exclusively owned by the Broker in Feature 9).

        Returns:
            (run_id, tenant_id, status, expected_version, lease_token) or None.
        """
        now_utc = self._db_now(session)
        stmt = (
            select(
                RunModel.id,
                RunModel.tenant_id,
                RunModel.status,
                RunModel.version,
                RunModel.lease_owner,
            )
            .where(
                or_(
                    RunModel.status == "queued",
                    (RunModel.status.in_(("investigating", "verifying")))
                    & (RunModel.lease_expires_at < now_utc),
                    (RunModel.status == "approved")
                    & (
                        (RunModel.lease_expires_at.is_(None))
                        | (RunModel.lease_expires_at < now_utc)
                    ),
                )
            )
            .order_by(RunModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = session.execute(stmt).first()
        if not row:
            return None

        run_id, tenant_id, run_status, version, prior_owner = row
        lease_token = uuid4()
        expires_at = now_utc + timedelta(seconds=self.lease_ttl_seconds)

        session.execute(
            update(RunModel)
            .where(RunModel.id == run_id)
            .values(
                worker_id=self.worker_id,
                lease_owner=self.worker_id,
                lease_token=lease_token,
                heartbeat_at=now_utc,
                lease_expires_at=expires_at,
            )
        )

        ctx = TenantContext(tenant_id=tenant_id, role="worker")
        if prior_owner is not None and run_status != "queued":
            RunService(session).append_event(
                ctx=ctx,
                run_id=run_id,
                event_type="run_reclaimed",
                actor=f"worker:{self.worker_id}",
                payload={
                    "previous_owner": prior_owner,
                    "new_owner": self.worker_id,
                    "run_status": run_status,
                },
            )
            worker_metrics.reclaimed_runs += 1
        else:
            RunService(session).append_event(
                ctx=ctx,
                run_id=run_id,
                event_type="worker_claimed",
                actor=f"worker:{self.worker_id}",
                payload={"worker_id": self.worker_id, "run_status": run_status},
            )

        session.commit()
        return run_id, tenant_id, run_status, version, lease_token

    def update_heartbeat(
        self, session: Session, run_id: UUID, lease_token: UUID | None = None
    ) -> None:
        """Refreshes worker heartbeat and lease expiry with lease_token fencing."""
        now_utc = self._db_now(session)
        expires_at = now_utc + timedelta(seconds=self.lease_ttl_seconds)
        if lease_token is not None:
            stmt = (
                update(RunModel)
                .where(
                    RunModel.id == run_id,
                    RunModel.lease_token == lease_token,
                )
                .values(heartbeat_at=now_utc, lease_expires_at=expires_at)
            )
            res = session.execute(stmt)
            if res.rowcount == 0:
                session.rollback()
                raise LeaseLostError(
                    f"Worker {self.worker_id} lost lease for run {run_id}",
                    details={"run_id": str(run_id), "worker_id": self.worker_id},
                )
            session.commit()
            return

        try:
            stmt = (
                update(RunModel)
                .where(RunModel.id == run_id)
                .values(heartbeat_at=now_utc, lease_expires_at=expires_at)
            )
            session.execute(stmt)
            session.commit()
        except Exception as exc:
            logger.warning("Failed to update heartbeat for run %s: %s", run_id, exc)

    def release_lease(
        self, session: Session, run_id: UUID, lease_token: UUID | None = None
    ) -> None:
        """Releases the worker lease cleanly (on interrupt wait, completion, or SIGTERM)."""
        stmt = (
            update(RunModel)
            .where(RunModel.id == run_id)
            .values(lease_owner=None, lease_token=None, lease_expires_at=None)
        )
        if lease_token is not None:
            stmt = stmt.where(RunModel.lease_token == lease_token)
        session.execute(stmt)
        session.commit()

    def reclaim_expired_run(self, session: Session) -> tuple[UUID, UUID, str, int, UUID] | None:
        """Reclaims an expired lease on an in-flight run using FOR UPDATE SKIP LOCKED."""
        now_utc = self._db_now(session)
        stmt = (
            select(
                RunModel.id,
                RunModel.tenant_id,
                RunModel.status,
                RunModel.version,
                RunModel.lease_owner,
            )
            .where(
                RunModel.status.in_(("investigating", "verifying")),
                RunModel.lease_expires_at < now_utc,
            )
            .order_by(RunModel.lease_expires_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = session.execute(stmt).first()
        if not row:
            return None

        run_id, tenant_id, run_status, version, prior_owner = row
        new_lease_token = uuid4()
        expires_at = now_utc + timedelta(seconds=self.lease_ttl_seconds)
        session.execute(
            update(RunModel)
            .where(RunModel.id == run_id)
            .values(
                worker_id=self.worker_id,
                lease_owner=self.worker_id,
                lease_token=new_lease_token,
                heartbeat_at=now_utc,
                lease_expires_at=expires_at,
            )
        )
        ctx = TenantContext(tenant_id=tenant_id, role="worker")
        RunService(session).append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="run_reclaimed",
            actor=f"worker:{self.worker_id}",
            payload={
                "previous_owner": prior_owner,
                "new_owner": self.worker_id,
                "run_status": run_status,
            },
        )
        session.commit()
        worker_metrics.reclaimed_runs += 1
        return run_id, tenant_id, run_status, version, new_lease_token

    def start_heartbeat_thread(
        self,
        run_id: UUID,
        lease_token: UUID,
        stop_event: threading.Event,
    ) -> threading.Thread:
        """Starts a background thread extending the lease every HEARTBEAT_SECONDS."""

        def _beat() -> None:
            while not stop_event.wait(self.heartbeat_seconds):
                hb_session = self.session_factory()
                try:
                    self.update_heartbeat(hb_session, run_id, lease_token)
                except LeaseLostError:
                    logger.warning("Heartbeat detected lease loss for run %s", run_id)
                    stop_event.set()
                    break
                except Exception as exc:
                    logger.warning("Heartbeat error for run %s: %s", run_id, exc)
                finally:
                    hb_session.close()

        t = threading.Thread(target=_beat, name=f"hb-{str(run_id)[:8]}", daemon=True)
        t.start()
        return t

    @staticmethod
    def resume_interrupted_run(
        graph: Any,
        run_id: UUID | str,
        decision_payload: dict[str, Any],
        recursion_limit: int = 50,
    ) -> InvestigationState | None:
        """Idempotently resumes a LangGraph thread paused at `await_review`.

        Checks checkpoint state first: if `await_review` is not pending in `snap.next`,
        returns None without invoking the graph (double resume is a safe no-op).
        """
        config = {
            "configurable": {"thread_id": str(run_id)},
            "recursion_limit": recursion_limit,
        }
        if hasattr(graph, "get_state"):
            snap = graph.get_state(config)
            pending_nodes = tuple(getattr(snap, "next", ()) or ())
            if "await_review" not in pending_nodes and "await_approval" not in pending_nodes:
                return None
        result = graph.invoke(Command(resume=decision_payload), config=config)
        worker_metrics.resumes_after_kill += 1
        return cast(InvestigationState, result)

    def resume_pending_decisions(
        self,
        *,
        graph_override: Any | None = None,
    ) -> list[UUID]:
        """Resume driver: polls DB for decided plans / cancelled runs and resumes waiting graphs.

        Reads the decision exclusively from the DB (never from HTTP). Idempotent:
        skips any run whose checkpoint is no longer waiting at `await_review`.
        """
        session = self.session_factory()
        resumed_ids: list[UUID] = []
        try:
            stmt = (
                select(RunModel, ActionPlanModel)
                .join(ActionPlanModel, ActionPlanModel.run_id == RunModel.id)
                .where(
                    RunModel.status.in_(("approved", "escalated")),
                    ActionPlanModel.status.in_(("approved", "rejected", "expired", "proposed")),
                )
                .order_by(RunModel.updated_at.asc())
            )
            rows = session.execute(stmt).all()
            if not rows:
                return []

            from agent.graph.nodes.await_review import DatabaseExecutionPort

            graph = graph_override or build_investigator_graph(
                llm_client=self.llm_client,
                run_service=RunService(session),
                tool_registry=self.tool_registry,
                handoff_port=self.handoff_port,
                checkpointer_type=self.checkpointer_type,
                postgres_conn_string=self.postgres_conn_string,
                execution_port=DatabaseExecutionPort(session),
            )

            for run_row, plan_row in rows:
                if plan_row.status == "approved":
                    exec_row = session.scalar(
                        select(ActionExecutionModel).where(ActionExecutionModel.plan_id == plan_row.id)
                    )
                    decision_payload: dict[str, Any] = {
                        "outcome": "approved",
                        "plan_id": str(plan_row.id),
                        "action_execution_id": str(exec_row.id) if exec_row else None,
                    }
                elif plan_row.status == "rejected":
                    decision_payload = {"outcome": "rejected", "plan_id": str(plan_row.id)}
                elif plan_row.status == "expired":
                    decision_payload = {"outcome": "expired", "plan_id": str(plan_row.id)}
                elif run_row.status == "escalated":
                    decision_payload = {"outcome": "cancelled", "plan_id": str(plan_row.id)}
                else:
                    continue

                res = self.resume_interrupted_run(graph, run_row.id, decision_payload)
                if res is not None:
                    ctx = TenantContext(tenant_id=run_row.tenant_id, role="worker")
                    RunService(session).append_event(
                        ctx=ctx,
                        run_id=run_row.id,
                        event_type="graph_resumed",
                        actor=f"worker:{self.worker_id}",
                        payload={
                            "plan_id": str(plan_row.id),
                            "outcome": decision_payload["outcome"],
                        },
                    )
                    session.commit()
                    resumed_ids.append(run_row.id)

            return resumed_ids
        except Exception as exc:
            session.rollback()
            logger.warning("Resume driver pass failed: %s", exc)
            return resumed_ids
        finally:
            session.close()

    def process_run(
        self,
        run_id: UUID,
        tenant_id: UUID,
        expected_version: int,
        lease_token: UUID | None = None,
        run_status: str = "queued",
    ) -> InvestigationState:
        """Processes a single claimed run under lease fencing and wall-clock timeout."""
        session = self.session_factory()
        tenant_ctx = TenantContext(tenant_id=tenant_id, role="worker")
        raw_run_service = RunService(session)
        effective_token = lease_token or uuid4()
        fenced_run_service: Any = (
            FencedRunService(raw_run_service, lease_token=effective_token)
            if lease_token is not None
            else raw_run_service
        )

        hb_stop = threading.Event()
        hb_thread = (
            self.start_heartbeat_thread(run_id, lease_token, hb_stop)
            if lease_token is not None
            else None
        )

        logger.info(
            "Worker %s starting run %s (tenant: %s, status: %s, version: %s)",
            self.worker_id,
            run_id,
            tenant_id,
            run_status,
            expected_version,
        )

        try:
            # 1. If queued, transition queued -> investigating via FencedRunService
            if run_status == "queued":
                fenced_run_service.transition(
                    ctx=tenant_ctx,
                    run_id=run_id,
                    to_status="investigating",
                    actor=f"worker:{self.worker_id}",
                    expected_version=expected_version,
                    payload={"worker_id": self.worker_id},
                )

            # 2. Fetch run and incident alert context
            run_row = raw_run_service.run_repo.get_by_id(tenant_ctx, run_id)
            incident_id = run_row.incident_id
            incident = session.get(IncidentModel, incident_id)
            alert_payload = incident.alert_payload if incident else {}

            initial_state = create_initial_state(
                run_id=str(run_id),
                tenant_id=str(tenant_id),
                incident_id=str(incident_id),
                alert=alert_payload,
                max_calls=run_row.max_calls,
            )

            # 3. Assemble investigation graph with FencedRunService
            effective_handoff = self.handoff_port or DefaultHandoffStub(fenced_run_service)
            graph = build_investigator_graph(
                llm_client=self.llm_client,
                run_service=fenced_run_service,
                tool_registry=self.tool_registry,
                handoff_port=effective_handoff,
                checkpointer_type=self.checkpointer_type,
                postgres_conn_string=self.postgres_conn_string,
            )

            # 4. Execute or resume from last checkpoint under wall-clock timeout
            with ThreadPoolExecutor(max_workers=1) as executor:
                if run_status == "investigating":
                    # Reclaimed investigating run: resume from last checkpoint
                    future = executor.submit(
                        graph.invoke,
                        None,
                        {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 50},
                    )
                else:
                    future = executor.submit(
                        run_investigation_graph,
                        graph,
                        initial_state,
                    )
                try:
                    final_state = future.result(timeout=self.timeout_seconds)
                except TimeoutError:
                    logger.error(
                        "Run %s timed out after %s seconds wall-clock limit",
                        run_id,
                        self.timeout_seconds,
                    )
                    timeout_error = {
                        "code": "timeout_exceeded",
                        "message": f"Run execution exceeded wall-clock timeout of {self.timeout_seconds}s",
                    }
                    fenced_run_service.record_error(
                        ctx=tenant_ctx,
                        run_id=run_id,
                        error=timeout_error,
                        actor="worker_timeout_watchdog",
                        target_status="escalated",
                    )
                    return {
                        **initial_state,
                        "status": "escalated",
                        "termination_reason": "timeout_exceeded",
                        "last_error": timeout_error,
                    }

            # 5. Release lease cleanly if paused at interrupt or on completion
            hb_stop.set()
            if hb_thread is not None:
                hb_thread.join(timeout=1.0)

            self.release_lease(session, run_id, lease_token)
            logger.info("Worker %s finished/checkpointed run %s", self.worker_id, run_id)
            return cast(InvestigationState, final_state)

        except LeaseLostError:
            hb_stop.set()
            logger.warning("Zombie worker %s fenced out of run %s", self.worker_id, run_id)
            try:
                raw_run_service.append_event(
                    ctx=tenant_ctx,
                    run_id=run_id,
                    event_type="worker_lease_lost",
                    actor=f"worker:{self.worker_id}",
                    payload={"previous_owner": self.worker_id},
                )
                session.commit()
            except Exception:
                session.rollback()
            raise

        except Exception as exc:
            hb_stop.set()
            logger.exception("Unexpected error processing run %s: %s", run_id, exc)
            try:
                fenced_run_service.record_error(
                    ctx=tenant_ctx,
                    run_id=run_id,
                    error={"code": "worker_crash", "message": str(exc)},
                    actor="worker_error_handler",
                    target_status="escalated",
                )
            except Exception as rec_err:
                logger.error("Failed to record error for crashed run %s: %s", run_id, rec_err)
            raise exc
        finally:
            hb_stop.set()
            if hb_thread is not None:
                hb_thread.join(timeout=1.0)
            session.close()

    def run_once(self) -> bool:
        """Polls once, claims a run if available, and processes it."""
        if not self._running and getattr(self, "_shutdown_requested", False):
            return False

        session = self.session_factory()
        claim: tuple[UUID, UUID, str, int, UUID] | None = None
        try:
            claim = self.claim_next_run(session)
        finally:
            session.close()

        if claim is None:
            return False

        run_id, tenant_id, run_status, version, lease_token = claim
        if run_status == "approved":
            self.resume_pending_decisions()
            s = self.session_factory()
            try:
                self.release_lease(s, run_id, lease_token)
            finally:
                s.close()
            return True

        self.process_run(
            run_id, tenant_id, version, lease_token=lease_token, run_status=run_status
        )
        return True

    def handle_sigterm(self, signum: int = signal.SIGTERM, _frame: Any = None) -> None:
        """Graceful SIGTERM/SIGINT handler: stops claiming new runs."""
        logger.info("Worker %s received signal %s; stopping claim loop gracefully", self.worker_id, signum)
        self._shutdown_requested = True
        self.stop()

    def install_signal_handlers(self) -> None:
        """Registers SIGTERM and SIGINT handlers on the main thread."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.handle_sigterm)
            signal.signal(signal.SIGINT, self.handle_sigterm)

    def sweep_expired_plans(self) -> list[UUID]:
        """Runs one pass of the overdue-plan expiry sweeper."""
        session = self.session_factory()
        try:
            repo = PlanRepository(session)
            expired_ids = repo.expire_overdue_plans(run_service=RunService(session))
            session.commit()
            return expired_ids
        except Exception as exc:
            session.rollback()
            logger.warning("Expiry sweep failed: %s", exc)
            return []
        finally:
            session.close()

    async def run_loop(self) -> None:
        """Continuous execution loop with configured concurrency and expiry sweeper."""
        self._running = True
        try:
            from apps.api.services.approval_service import check_clock_skew

            s = self.session_factory()
            try:
                check_clock_skew(s)
            finally:
                s.close()
        except Exception as exc:
            logger.warning("Worker startup clock sanity check skipped: %s", exc)

        logger.info(
            "Worker %s loop started (concurrency: %s, timeout: %ss, sweep: %ss)",
            self.worker_id,
            self.concurrency,
            self.timeout_seconds,
            self.expiry_sweep_interval_seconds,
        )

        loop = asyncio.get_running_loop()
        last_sweep = 0.0

        while self._running:
            now = loop.time()
            if now - last_sweep >= self.expiry_sweep_interval_seconds:
                last_sweep = now
                try:
                    await asyncio.to_thread(self.sweep_expired_plans)
                except Exception as exc:
                    logger.error("Error in worker expiry sweep: %s", exc)

            try:
                await asyncio.to_thread(self.resume_pending_decisions)
            except Exception as exc:
                logger.error("Error in worker resume driver: %s", exc)

            did_work = False
            try:
                did_work = await asyncio.to_thread(self.run_once)
            except Exception as exc:
                logger.error("Error in worker polling loop: %s", exc)

            if not did_work:
                await asyncio.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        """Stops the worker polling loop."""
        self._running = False


async def main() -> None:
    """Worker daemon entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    worker = WorkerService()
    try:
        await worker.run_loop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker shutting down...")
        worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
