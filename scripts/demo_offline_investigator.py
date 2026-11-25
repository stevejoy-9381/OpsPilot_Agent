"""scripts/demo_offline_investigator.py

Demonstration script running the OpsPilot Feature 6 Investigator Worker offline (v2).
Creates an incident, runs the graph with a scripted v2 trajectory, and prints
the real-time event stream including hypothesis ledger and gate events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import MemorySaver

from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.state import create_initial_state
from apps.api.domain.context import TenantContext
from baseline.alerts.models import AlertEvent
from tests.test_v2_loop_deterministic import (
    FIXTURES_V2_DIR,
    ScriptedV2LLMClient,
    _build_snapshot_fixture,
    _InMemoryRepo,
    _InMemoryRunService,
    _MockRunRecord,
)
from tools.registry import _CALL_CACHE


def main() -> None:
    print("=" * 80)
    print("OPSPILOT INCIDENT INVESTIGATOR (v2): OFFLINE DEMONSTRATION")
    print("=" * 80)

    _CALL_CACHE.clear()

    # 1. Setup isolated snapshot telemetry fixtures
    tmp_dir = Path("evals/results/investigator/demo_fixtures")
    base_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    backend = _build_snapshot_fixture(tmp_dir, base_time, "S1")

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

    # 3. Load Scripted v2 Trajectory Fixture
    traj_file = FIXTURES_V2_DIR / "S1_good.json"
    scripted_steps = json.loads(traj_file.read_text(encoding="utf-8"))
    llm_client = ScriptedV2LLMClient(steps_data=scripted_steps)

    # 4. Setup InMemory RunService and Repo
    repo = _InMemoryRepo()
    run_service = _InMemoryRunService(repo)
    tenant_ctx = TenantContext(tenant_id=tenant_id)

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

    # 5. Build and Execute Graph (v2)
    print("\n[3] Executing Investigator Graph v2 (LangGraph + Ledger + Gate)...")
    print("-" * 80)
    print(f"{'Seq':<5} | {'Event Type':<27} | {'Actor':<15} | Details")
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
        handoff_port=DefaultHandoffStub(run_service=run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        backend=backend,
    )

    final_state = run_investigation_graph(
        graph=compiled_graph,
        initial_state=initial_state,
    )

    # Print emitted event stream including ledger and gate events
    for ev in repo.events:
        seq = ev.get("seq")
        ev_type = ev.get("event_type")
        actor = ev.get("actor")
        payload = ev.get("payload", {})
        if ev_type == "agent_step":
            details = f"{payload.get('step_kind')}({payload.get('tool') or ''}) - {str(payload.get('reason', ''))[:55]}"
        elif ev_type == "tool_call":
            details = f"tool={payload.get('tool')} ok={payload.get('ok')} evidence={payload.get('evidence_id')}"
        elif ev_type in ("hypothesis_added", "hypothesis_status_updated", "evidence_linked", "ledger_updated"):
            details = json.dumps(payload)[:80]
        elif ev_type == "diagnosis_persisted":
            details = f"outcome={payload.get('outcome_kind')}"
        elif ev_type == "status_transition":
            details = f"to_status={payload.get('to_status')} reason={payload.get('reason')}"
        else:
            details = json.dumps(payload)[:80]
        print(f"{seq:<5} | {ev_type:<27} | {actor:<15} | {details}")

    print("-" * 80)

    # 6. Final Diagnosis & Ledger Summary
    diag = final_state.get("draft_diagnosis") or {}
    ledger = final_state.get("hypotheses_ledger") or {}
    print("\n[4] Final Diagnosis & Hypothesis Ledger Summary:")
    print(f"    Outcome:         {diag.get('outcome_kind')} ({diag.get('outcome_reason')})")
    print(f"    Category:        {diag.get('root_cause_category')}")
    print(f"    Service:         {diag.get('root_cause_service')}")
    print(f"    Mechanism:       {diag.get('mechanism')}")
    print(f"    Culprit Deploy:  {diag.get('culprit_deploy_id')}")
    print(f"    Confidence:      {diag.get('capped_confidence')} (model={diag.get('model_confidence')})")
    print(f"    Evidence Cited:  {diag.get('evidence_ids')}")
    print(f"    Hypotheses ({len(ledger)}):  {[(hid, h.get('status'), h.get('category')) for hid, h in ledger.items()]}")
    print(f"    Gate Rejections: {final_state.get('gate_rejection_count', 0)}")
    print(f"    Status:          {final_state.get('status')}")
    print(f"    Termination:     {final_state.get('termination_reason')}")
    print("\n[OK] Offline Investigator v2 Demo Completed Successfully.")


if __name__ == "__main__":
    main()
