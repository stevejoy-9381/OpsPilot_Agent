"""scripts/demo_offline_investigator.py

Demonstration script running OpsPilot Feature 6+7 Investigator & Proposal offline (v2 -> propose -> needs_review).
Creates an incident, runs the graph with a scripted v2 trajectory + ProposalDecision,
persists the ActionPlan, and prints the event stream and review payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import MemorySaver

from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.nodes.propose import ProposalDecision
from agent.graph.state import create_initial_state
from agent.llm.interface import Message, StructuredResult, TokenUsage
from agent.policy import compute_policy_version
from apps.api.domain.context import TenantContext
from apps.api.services.plan_review import build_plan_review_payload
from baseline.alerts.models import AlertEvent
from shared.actions import ActionType
from shared.plans import ActionPlan
from tests.test_v2_loop_deterministic import (
    FIXTURES_V2_DIR,
    ScriptedV2LLMClient,
    _build_snapshot_fixture,
    _InMemoryRepo,
    _InMemoryRunService,
    _MockRunRecord,
)
from tools.read.target_state import SnapshotTargetReader
from tools.registry import _CALL_CACHE


class _DemoV2AndProposalLLM(ScriptedV2LLMClient):
    """Extends ScriptedV2LLMClient to also return ProposalDecision during propose_node."""

    def __init__(self, steps_data: list[dict[str, Any]], proposal: ProposalDecision) -> None:
        super().__init__(steps_data=steps_data)
        self._proposal = proposal

    def generate_structured(
        self,
        messages: list[Message],
        response_schema: type[Any],
        *,
        max_output_tokens: int = 2048,
        timeout_s: float = 30.0,
        **_kwargs: Any,
    ) -> StructuredResult[Any]:
        if response_schema.__name__ == "ProposalDecision":
            proposal = self._proposal
            if self.captured_evidence_ids:
                proposal = proposal.model_copy(
                    update={"rationale_evidence_ids": list(self.captured_evidence_ids[:4])}
                )
            return StructuredResult(
                parsed=proposal,
                token_usage=TokenUsage(input_tokens=60, output_tokens=30, total_tokens=90),
                model="scripted-demo",
            )
        return super().generate_structured(
            messages=messages,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )


def main() -> None:
    print("=" * 80)
    print("OPSPILOT INCIDENT INVESTIGATOR + PROPOSAL (v2 -> needs_review): OFFLINE DEMO")
    print("=" * 80)

    _CALL_CACHE.clear()

    # 1. Setup isolated snapshot telemetry fixtures & target state reader
    tmp_dir = Path("evals/results/investigator/demo_fixtures")
    base_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    backend = _build_snapshot_fixture(tmp_dir, base_time, "S1")
    deploy_history = [
        {
            "deploy_id": "deploy_orders_v100",
            "service": "orders",
            "old_version": "v0.9.0",
            "new_version": "v1.0.0",
            "deployed_at": (base_time - timedelta(hours=2)).isoformat(),
        },
        {
            "deploy_id": "deploy_orders_v130",
            "service": "orders",
            "old_version": "v1.0.0",
            "new_version": "v1.3.0",
            "deployed_at": (base_time - timedelta(minutes=3)).isoformat(),
        },
    ]
    target_reader = SnapshotTargetReader(
        snapshot_dir=tmp_dir,
        deploy_history=deploy_history,
        snapshot_read_at=base_time,
        max_age_seconds=3600,
    )

    # 2. Ingest Alert and Create Incident & Run
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    incident_id = uuid4()
    run_id = uuid4()

    alert = AlertEvent(
        alert_id="alt_s1_demo_001",
        fired_at=base_time,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=10.0,
        window_seconds=300,
        severity="CRITICAL",
        status="firing",
    )

    print("\n[1] Incident Ingested:")
    print(f"    Incident ID: {incident_id}")
    print(f"    Run ID:      {run_id}")
    print(f"    Alert:       {alert.rule_id} on '{alert.service}' (observed: {alert.observed_value}, threshold: {alert.threshold})")

    # 3. Load Scripted v2 Trajectory Fixture + ProposalDecision
    traj_file = FIXTURES_V2_DIR / "S1_good.json"
    scripted_steps = json.loads(traj_file.read_text(encoding="utf-8"))
    proposal_decision = ProposalDecision(
        decision="propose",
        action=ActionType.ROLLBACK_SERVICE,
        service="orders",
        expected_version="v1.3.0",
        rationale_evidence_ids=["ev_logs_s1", "ev_deploys_s1"],
        note="Roll back orders v1.3.0 to v1.0.0 to restore DB connection pool.",
    )
    llm_client = _DemoV2AndProposalLLM(steps_data=scripted_steps, proposal=proposal_decision)

    # 4. Setup InMemory RunService and Repo
    repo = _InMemoryRepo()
    run_service = _InMemoryRunService(repo)
    tenant_ctx = TenantContext(tenant_id=tenant_id)
    persisted_plans: list[ActionPlan] = []

    run_rec = _MockRunRecord(run_id=run_id, tenant_id=tenant_id, status="queued")
    run_rec.incident_id = incident_id
    repo.runs[run_id] = run_rec

    print("\n[2] Investigator Worker Claiming Queued Run...")
    run_rec, _ = run_service.transition(
        ctx=tenant_ctx,
        run_id=run_id,
        to_status="investigating",
        actor="worker-demo",
        expected_version=run_rec.version,
    )
    print("    Worker ID:   worker-demo-01")
    print(f"    Status:      {run_rec.status}")

    # 5. Build and Execute Graph (v2 -> propose -> needs_review)
    print("\n[3] Executing Investigator Graph v2 -> Propose -> Policy Validator...")
    print("-" * 80)
    print(f"{'Seq':<5} | {'Event Type':<27} | {'Actor':<20} | Details")
    print("-" * 80)

    initial_state = create_initial_state(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        alert=alert,
        max_calls=8,
        schema_version="v2",
    )

    compiled_graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        handoff_port=None,
        checkpointer=MemorySaver(),
        backend=backend,
        target_reader=target_reader,
        plan_persister=lambda _ctx, p, _sup: persisted_plans.append(p),
        db_clock_fn=lambda: datetime.now(timezone.utc),
    )

    final_state = run_investigation_graph(
        graph=compiled_graph,
        initial_state=initial_state,
    )

    for ev in repo.events:
        seq = ev.get("seq")
        ev_type = ev.get("event_type")
        actor = ev.get("actor")
        payload = ev.get("payload", {})
        if ev_type == "agent_step":
            details = f"{payload.get('step_kind')}({payload.get('tool') or ''}) - {str(payload.get('reason', ''))[:45]}"
        elif ev_type == "tool_call":
            details = f"tool={payload.get('tool')} ok={payload.get('ok')} evidence={payload.get('evidence_id')}"
        elif ev_type == "status_transition":
            details = f"to_status={payload.get('to_status')} reason={payload.get('reason')}"
        else:
            details = json.dumps(payload)[:80]
        print(f"{seq:<5} | {ev_type:<27} | {actor:<20} | {details}")

    print("-" * 80)

    diag = final_state.get("draft_diagnosis") or {}
    print("\n[4] Final Run & Proposal Summary:")
    print(f"    Status:          {final_state.get('status')}")
    print(f"    Outcome:         {diag.get('outcome_kind')} ({diag.get('outcome_reason')})")
    print(f"    Category/Mech:   {diag.get('root_cause_category')} / {diag.get('mechanism')}")

    if persisted_plans:
        p = persisted_plans[-1]
        plan_ns = SimpleNamespace(
            id=uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            action=p.action.value,
            service=p.service,
            target_id=p.target_id,
            expected_version=p.expected_version,
            rollback_to_version=p.rollback_to_version,
            rationale_evidence_ids=p.rationale_evidence_ids,
            rollback_route=p.rollback_route,
            plan_hash=p.plan_hash,
            policy_version=compute_policy_version(),
            status=p.status.value,
            notes=p.note,
            expires_at=p.expires_at,
        )
        diag_data = dict(diag)
        if diag_data.get("model_confidence") is not None and not isinstance(diag_data["model_confidence"], str):
            diag_data["model_confidence"] = str(diag_data["model_confidence"])
        diag_ns = SimpleNamespace(**diag_data)
        obs_list = repo.observations.get(run_id, [])
        review_payload = build_plan_review_payload(
            plan=plan_ns,
            diagnosis=diag_ns,
            observations=obs_list,
            hypotheses=[],
        )
        print("\n[5] Review Screen Payload (GET /plans/{id}/review):")
        print(json.dumps(review_payload.model_dump(mode="json"), indent=2))

    print("\n[OK] Offline Investigator + Proposal Demo Completed Successfully.")


if __name__ == "__main__":
    main()
