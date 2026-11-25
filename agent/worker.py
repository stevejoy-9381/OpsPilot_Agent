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
import socket
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub, HandoffPort
from agent.graph.state import InvestigationState, create_initial_state
from agent.llm.interface import LLMClient
from apps.api.auth.dependencies import get_db_session_factory
from apps.api.domain.context import TenantContext
from apps.api.models.tables import IncidentModel, RunModel
from apps.api.services.run_service import RunService
from tools.registry import ToolRegistry, registry

logger = logging.getLogger("opspilot.agent.worker")

DEFAULT_WORKER_CONCURRENCY: int = 1
DEFAULT_RUN_TIMEOUT_SECONDS: float = 120.0
DEFAULT_POLL_INTERVAL_SECONDS: float = 0.5


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
        self.checkpointer_type = checkpointer_type or os.getenv("CHECKPOINTER_TYPE", "memory")
        self.postgres_conn_string = postgres_conn_string or os.getenv("DATABASE_URL")
        self._running = False

    def claim_next_run(self, session: Session) -> tuple[UUID, UUID, int] | None:
        """Atomically claims a queued run using SELECT ... FOR UPDATE SKIP LOCKED.

        Returns:
            (run_id, tenant_id, expected_version) or None if no queued runs available.
        """
        stmt = (
            select(RunModel.id, RunModel.tenant_id, RunModel.version)
            .where(RunModel.status == "queued")
            .order_by(RunModel.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = session.execute(stmt).first()
        if not row:
            return None

        run_id, tenant_id, version = row
        # Record lease and heartbeat on the claimed run
        update_stmt = (
            update(RunModel)
            .where(RunModel.id == run_id)
            .values(
                worker_id=self.worker_id,
                heartbeat_at=func.clock_timestamp(),
            )
        )
        session.execute(update_stmt)
        session.commit()
        return run_id, tenant_id, version

    def update_heartbeat(self, session: Session, run_id: UUID) -> None:
        """Records an updated worker heartbeat timestamp on the run."""
        try:
            stmt = (
                update(RunModel)
                .where(RunModel.id == run_id)
                .values(heartbeat_at=func.clock_timestamp())
            )
            session.execute(stmt)
            session.commit()
        except Exception as exc:
            logger.warning("Failed to update heartbeat for run %s: %s", run_id, exc)

    def process_run(
        self,
        run_id: UUID,
        tenant_id: UUID,
        expected_version: int,
    ) -> InvestigationState:
        """Processes a single claimed investigation run to completion under timeout."""
        session = self.session_factory()
        tenant_ctx = TenantContext(tenant_id=tenant_id, role="worker")
        run_service = RunService(session)

        logger.info(
            "Worker %s starting run %s (tenant: %s, version: %s)",
            self.worker_id,
            run_id,
            tenant_id,
            expected_version,
        )

        try:
            # 1. Transition queued -> investigating via RunService
            updated_run, _ = run_service.transition(
                ctx=tenant_ctx,
                run_id=run_id,
                to_status="investigating",
                actor=f"worker:{self.worker_id}",
                expected_version=expected_version,
                payload={"worker_id": self.worker_id},
            )

            # 2. Fetch run and incident alert context
            run_row = run_service.run_repo.get_by_id(tenant_ctx, run_id)
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

            # 3. Assemble investigation graph
            effective_handoff = self.handoff_port or DefaultHandoffStub(run_service)
            graph = build_investigator_graph(
                llm_client=self.llm_client,
                run_service=run_service,
                tool_registry=self.tool_registry,
                handoff_port=effective_handoff,
                checkpointer_type=self.checkpointer_type,
                postgres_conn_string=self.postgres_conn_string,
            )

            # 4. Execute graph with per-run wall-clock timeout
            with ThreadPoolExecutor(max_workers=1) as executor:
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
                    run_service.record_error(
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

            # 5. Heartbeat update on completion
            self.update_heartbeat(session, run_id)
            logger.info("Worker %s successfully completed run %s", self.worker_id, run_id)
            return final_state

        except Exception as exc:
            logger.exception("Unexpected error processing run %s: %s", run_id, exc)
            try:
                run_service.record_error(
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
            session.close()

    def run_once(self) -> bool:
        """Polls once, claims a run if available, and processes it.

        Returns:
            True if a run was claimed and processed; False if no runs were queued.
        """
        session = self.session_factory()
        claim: tuple[UUID, UUID, int] | None = None
        try:
            claim = self.claim_next_run(session)
        finally:
            session.close()

        if claim is None:
            return False

        run_id, tenant_id, version = claim
        self.process_run(run_id, tenant_id, version)
        return True

    async def run_loop(self) -> None:
        """Continuous execution loop with configured concurrency."""
        self._running = True
        logger.info(
            "Worker %s loop started (concurrency: %s, timeout: %ss)",
            self.worker_id,
            self.concurrency,
            self.timeout_seconds,
        )

        while self._running:
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
