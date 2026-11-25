"""scripts/demo_offline_investigator.py

Demonstration script running the OpsPilot Investigator Worker offline.
Creates an incident, runs the worker with a scripted client, and prints the real-time event stream.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.state import create_initial_state
from apps.api.domain.context import TenantContext
from baseline.alerts.models import AlertEvent
from tests.test_investigator_loop_deterministic import (
    InMemoryRunRepo,
    InMemoryRunService,
    MockRunRecord,
    TrajectoryLLMClient,
    build_test_snapshot_environment,
)


def main() -> None:
    print("=" * 80)
    print("OPSPILOT INCIDENT INVESTIGATOR: OFFLINE DEMONSTRATION")
    print("=" * 80)

    # 1. Setup isolated snapshot telemetry fixtures
    tmp_dir = Path("evals/results/investigator/demo_fixtures")
    build_test_snapshot_environment(tmp_dir)

    # 2. Ingest Alert and Create Incident & Run
    base_time = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    incident_id = uuid4()
    run_id = uuid4()

    alert = AlertEvent(
        alert_id="alt_s1_demo_001",
        fired_at=base_time,
        service="orders",
        rule_id="orders_db_pool_exhaustion",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=9.5,
        window_seconds=300,
        severity="CRITICAL",
        status="firing",
    )

    print("\n[1] Incident Ingested:")
    print(f"    Incident ID: {incident_id}")
    print(f"    Run ID:      {run_id}")
    print(f"    Alert:       {alert.rule_id} on '{alert.service}' (observed: {alert.observed_value}, threshold: {alert.threshold})")

    # 3. Load Scripted Trajectory Fixture
    traj_file = Path("tests/fixtures/trajectories/s1_trajectory.json")
    scripted_steps = json.loads(traj_file.read_text(encoding="utf-8"))
    llm_client = TrajectoryLLMClient(steps_data=scripted_steps)

    # 4. Setup InMemory RunService and Repo
    repo = InMemoryRunRepo()
    run_service = InMemoryRunService(repo)
    tenant_ctx = TenantContext(tenant_id=tenant_id)

    # Create run record in repo
    run_rec = MockRunRecord(run_id=run_id, tenant_id=tenant_id, status="queued")
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

    # 5. Build and Execute Graph
    print("\n[3] Executing Investigator Graph (LangGraph Engine)...")
    print("-" * 80)
    print(f"{'Seq':<5} | {'Event Type':<25} | {'Actor':<15} | Details")
    print("-" * 80)

    # Initial state
    initial_state = create_initial_state(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        alert=alert,
        max_calls=8,
    )

    compiled_graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        handoff_port=DefaultHandoffStub(run_service=run_service),  # type: ignore[arg-type]
        checkpointer=None,
    )

    final_state = run_investigation_graph(
        graph=compiled_graph,
        initial_state=initial_state,
    )

    # Print emitted event stream in real-time order
    for ev in repo.events:
        seq = ev.get("seq")
        ev_type = ev.get("event_type")
        actor = ev.get("actor")
        payload = ev.get("payload", {})
        details = ""
        if ev_type == "agent_step":
            details = f"{payload.get('step_kind')}({payload.get('tool') or ''}) - {payload.get('reason')[:60]}..."
        elif ev_type == "tool_call":
            details = f"tool={payload.get('tool')} ok={payload.get('ok')} evidence={payload.get('evidence_id')}"
        elif ev_type == "diagnosis_persisted":
            details = f"category={payload.get('root_cause_category')} service={payload.get('root_cause_service')} evidence={payload.get('evidence_ids')}"
        elif ev_type == "status_transition":
            details = f"to_status={payload.get('to_status')} reason={payload.get('reason')}"
        else:
            details = json.dumps(payload)[:70]
        print(f"{seq:<5} | {ev_type:<25} | {actor:<15} | {details}")

    print("-" * 80)

    # 6. Final Diagnosis Summary
    diag = final_state.get("draft_diagnosis") or {}
    print("\n[4] Final Diagnosis Artifact (Feature 1.12 Schema):")
    print(f"    Category:        {diag.get('root_cause_category')}")
    print(f"    Service:         {diag.get('root_cause_service')}")
    print(f"    Mechanism:       {diag.get('mechanism')}")
    print(f"    Culprit Deploy:  {diag.get('culprit_deploy_id')}")
    print(f"    Confidence:      {diag.get('confidence')}")
    print(f"    Evidence Cited:  {diag.get('evidence_ids')}")
    print(f"    Status:          {final_state.get('status')}")
    print(f"    Termination:     {final_state.get('termination_reason')}")
    print("\n[OK] Offline Investigator Demo Completed Successfully.")


if __name__ == "__main__":
    main()
