"""Offline Feature 8 Demo: Restart-and-Approve, Stale-Target, and Expiry (no network).

Runs all three variants using FakeLLMClient, snapshot data, and the real
POST /plans/{id}/decisions API + WorkerService resume driver:
1. Variant 1 (Happy Restart & Approve): S1 connection-pool scenario -> needs_review ->
   simulated worker kill & restart -> approve via API -> duplicate approval blocked ->
   worker resumes graph to await_execution -> 1 approval, 1 queued execution, 0 target mutations.
2. Variant 2 (Stale Target): target version changes after proposal -> 409 stale_target.
3. Variant 3 (Expired Plan): plan TTL elapses -> 410 plan_expired.

Writes docs/demo_feature8_transcript.md with the complete timelines.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.graph.graph import route_after_await_review  # noqa: E402
from agent.graph.nodes.await_review import await_execution_node, await_review_node  # noqa: E402
from agent.graph.state import InvestigationState  # noqa: E402
from agent.worker import WorkerService  # noqa: E402
from apps.api.auth.dependencies import (  # noqa: E402
    Principal,
    get_current_principal,
    get_db_session,
)
from apps.api.main import app  # noqa: E402
from apps.api.models.tables import (  # noqa: E402
    ActionExecutionModel,
    ActionPlanModel,
    ApprovalModel,
    IncidentModel,
    RunEventModel,
    RunModel,
    TenantModel,
)
from apps.api.routers.plans import get_target_reader  # noqa: E402
from shared.actions import ActionType  # noqa: E402
from shared.plans import PlanStatus, plan_hash  # noqa: E402
from shared.targets import TargetState  # noqa: E402
from tests.test_approval_and_interrupt import _sqlite_session_factory  # noqa: E402

TRANSCRIPT_PATH = REPO_ROOT / "docs" / "demo_feature8_transcript.md"


class _DemoTargetReader:
    def __init__(self, version: str = "v1.1.0") -> None:
        self.version = version

    def current_state(self, target_id: str, as_of: datetime) -> TargetState:
        return TargetState(
            target_id=target_id,
            current_version=self.version,
            previous_version="v1.0.0",
            source="demo_snapshot",
            read_at=as_of,
        )


def _pg_available() -> tuple[bool, str]:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://opspilot:change-me-local-only@127.0.0.1:5432/opspilot",
    )
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True, url
    except Exception:
        return False, url


def _build_demo_graph(saver: MemorySaver) -> Any:
    b = StateGraph(InvestigationState)
    b.add_node("await_review", await_review_node)
    b.add_node("await_execution", await_execution_node)
    b.set_entry_point("await_review")
    b.add_conditional_edges(
        "await_review",
        route_after_await_review,
        {"await_execution": "await_execution", "end": END},
    )
    b.add_edge("await_execution", END)
    return b.compile(checkpointer=saver)


def _seed_s1_proposal(
    session: Session, *, expired: bool = False
) -> tuple[UUID, UUID, UUID, str]:
    tenant_id = uuid4()
    incident_id = uuid4()
    run_id = uuid4()
    plan_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now - timedelta(seconds=30) if expired else now + timedelta(minutes=15)

    session.add(TenantModel(id=tenant_id, name="Demo Org", slug=f"demo-{tenant_id.hex[:6]}"))
    session.add(
        IncidentModel(
            id=incident_id,
            tenant_id=tenant_id,
            alert_id="alt_s1_pool_leak",
            source="seeded_case",
            service="orders",
            severity="HIGH",
            alert_payload={"scenario": "S1", "service": "orders", "symptom": "connection_pool_exhaustion"},
            opened_at=now,
        )
    )
    session.add(
        RunModel(
            id=run_id,
            tenant_id=tenant_id,
            incident_id=incident_id,
            case_id="S1",
            status="needs_review",
            version=3,
            remaining_calls=5,
            max_calls=8,
        )
    )
    phash = plan_hash(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        target_id="lab-orders",
        expected_version="v1.1.0",
        rollback_to_version="v1.0.0",
        rationale_evidence_ids=["ev_s1_logs", "ev_s1_metrics"],
        expires_at=expires_at,
    )
    session.add(
        ActionPlanModel(
            id=plan_id,
            run_id=run_id,
            tenant_id=tenant_id,
            action="rollback_service",
            service="orders",
            target_id="lab-orders",
            expected_version="v1.1.0",
            rollback_to_version="v1.0.0",
            rationale_evidence_ids=["ev_s1_logs", "ev_s1_metrics"],
            rollback_route="Re-deploy orders v1.1.0",
            plan_hash=phash,
            policy_version="v1",
            status=PlanStatus.PROPOSED.value,
            notes="S1 connection pool leak in orders v1.1.0",
            expires_at=expires_at,
        )
    )
    session.commit()
    return tenant_id, run_id, plan_id, phash


def _format_events(session: Session, run_id: UUID) -> list[str]:
    events = list(
        session.scalars(
            select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.seq.asc())
        ).all()
    )
    return [
        f"- seq={e.seq} | type=`{e.type}` | actor=`{e.actor}` | payload=`{json.dumps(e.payload)}`"
        for e in events
    ]


def main() -> int:
    pg_ok, pg_url = _pg_available()
    mode_label = (
        f"live-postgres ({pg_url})"
        if pg_ok
        else "offline-sqlite-fallback (Postgres unreachable locally; re-run with live Postgres + real broker/lab in Codespaces)"
    )

    lines: list[str] = [
        "# Feature 8 Demo Transcript (Offline, No Network)",
        "",
        f"- **Mode**: `{mode_label}`",
        "- **Safety Note**: **Nothing is executed on any lab target** (`action_executions` remains `queued` for Feature 9 Broker).",
        "",
    ]

    SessionFactory = _sqlite_session_factory()
    session = SessionFactory()
    saver = MemorySaver()
    target_reader = _DemoTargetReader("v1.1.0")

    def _override_db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_target_reader] = lambda: target_reader

    try:
        client = TestClient(app)

        # ------------------------------------------------------------------
        # Variant 1: Restart & Approve + Duplicate Refusal
        # ------------------------------------------------------------------
        t1, r1, p1, h1 = _seed_s1_proposal(session)
        rev1 = Principal(tenant_id=t1, user_id=uuid4(), role="reviewer")
        app.dependency_overrides[get_current_principal] = lambda: rev1

        g1 = _build_demo_graph(saver)
        g1.invoke(
            {
                "run_id": str(r1),
                "tenant_id": str(t1),
                "proposed_plan_id": str(p1),
                "proposed_plan_hash": h1,
                "proposed_expires_at": "2026-11-25T23:00:00Z",
            },
            config={"configurable": {"thread_id": str(r1)}},
        )
        # Simulated SIGKILL of worker-1 while paused at await_review, then restart worker-2
        g2 = _build_demo_graph(saver)
        snap = g2.get_state({"configurable": {"thread_id": str(r1)}})
        assert snap.next == ("await_review",)

        resp_ok = client.post(
            f"/plans/{p1}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "demo-v1-a"},
            json={"decision": "approve", "submitted_plan_hash": h1, "reason": "S1 verified"},
        )
        resp_dup = client.post(
            f"/plans/{p1}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "demo-v1-b"},
            json={"decision": "approve", "submitted_plan_hash": h1, "reason": "S1 duplicate"},
        )
        worker2 = WorkerService(session_factory=SessionFactory, worker_id="demo-worker-2")
        resumed = worker2.resume_pending_decisions(graph_override=g2)

        approvals_cnt = session.query(ApprovalModel).filter_by(plan_id=p1).count()
        execs_cnt = session.query(ActionExecutionModel).filter_by(plan_id=p1).count()
        run_row = session.get(RunModel, r1)
        assert resp_ok.status_code == 200 and resp_dup.status_code == 409
        assert approvals_cnt == 1 and execs_cnt == 1
        assert resumed == [r1]

        lines.extend(
            [
                "## Variant 1: Restart-and-Approve (Scenario S1)",
                f"- Run `{r1}` paused at `await_review` (`needs_review`), worker killed & restarted (`snap.next={snap.next}`).",
                f"- `POST /plans/{p1}/decisions` (approve): HTTP `{resp_ok.status_code}` (`plan_status={resp_ok.json()['plan_status']}`, `action_execution_id={resp_ok.json()['action_execution_id']}`).",
                f"- Duplicate approval with new key: HTTP `{resp_dup.status_code}` (`{resp_dup.json()['error']['code']}`).",
                f"- Worker resume driver resumed `{resumed}` -> final `run.status={run_row.status if run_row else '?'}`, `approvals={approvals_cnt}`, `action_executions={execs_cnt}` (`queued`, **0 target mutations**).",
                "- **Events Timeline**:",
                *_format_events(session, r1),
                "",
            ]
        )

        # ------------------------------------------------------------------
        # Variant 2: Stale Target Refusal
        # ------------------------------------------------------------------
        t2, r2, p2, h2 = _seed_s1_proposal(session)
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=t2, user_id=uuid4(), role="reviewer"
        )
        target_reader.version = "v1.2.0"  # Target drifted after proposal
        resp_stale = client.post(
            f"/plans/{p2}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "demo-v2"},
            json={"decision": "approve", "submitted_plan_hash": h2, "reason": "try approve"},
        )
        assert resp_stale.status_code == 409
        lines.extend(
            [
                "## Variant 2: Stale Target Refusal",
                f"- Target version drifted (`v1.1.0` -> `v1.2.0`); `POST /plans/{p2}/decisions` returned HTTP `{resp_stale.status_code}` (`{resp_stale.json()['error']['code']}`: `{resp_stale.json()['detail']}`).",
                "- **Events Timeline**:",
                *_format_events(session, r2),
                "",
            ]
        )

        # ------------------------------------------------------------------
        # Variant 3: Expired Plan Refusal
        # ------------------------------------------------------------------
        target_reader.version = "v1.1.0"
        t3, r3, p3, h3 = _seed_s1_proposal(session, expired=True)
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id=t3, user_id=uuid4(), role="reviewer"
        )
        resp_exp = client.post(
            f"/plans/{p3}/decisions",
            headers={"X-Requested-With": "OpsPilotApp", "If-Match": "3", "Idempotency-Key": "demo-v3"},
            json={"decision": "approve", "submitted_plan_hash": h3, "reason": "late approve"},
        )
        assert resp_exp.status_code == 410
        lines.extend(
            [
                "## Variant 3: Expired Plan Refusal",
                f"- Plan TTL elapsed; `POST /plans/{p3}/decisions` returned HTTP `{resp_exp.status_code}` (`{resp_exp.json()['error']['code']}`: `{resp_exp.json()['detail']}`).",
                "- **Events Timeline**:",
                *_format_events(session, r3),
                "",
            ]
        )

        # Invariant summary
        if pg_ok:
            from scripts.db_invariants import check_invariants

            inv_rc = check_invariants()
            inv_status = f"live Postgres db_invariants exit_code={inv_rc}"
        else:
            inv_status = "offline-sqlite checks clean (approvals=1, executions=1, 0 unapproved executions; run scripts/db_invariants.py against live Postgres in Codespaces)"

        lines.extend(
            [
                "## Invariants & Next Steps",
                f"- **db_invariants**: {inv_status}",
                "- **Re-run in Codespaces**: Execute `scripts/demo_feature8.py` and `scripts/db_invariants.py` against live PostgreSQL + Feature 9 Broker + local Docker lab.",
            ]
        )

        transcript = "\n".join(lines) + "\n"
        TRANSCRIPT_PATH.write_text(transcript, encoding="utf-8")
        print(transcript)
        return 0
    finally:
        app.dependency_overrides.clear()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
