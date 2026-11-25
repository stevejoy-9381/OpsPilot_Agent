"""evals/investigator_runner.py

Runs the real OpsPilot Investigator Agent (Feature 5) with live Gemini model across
development scenarios S1-S5 (trials=1) on SnapshotBackend telemetry fixtures.
Saves raw trajectories and summary under evals/results/investigator/<timestamp>/.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from agent.graph.context import EXPECTED_PROMPT_V1_HASH, compute_prompt_hash, load_system_prompt
from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.state import create_initial_state
from agent.llm.gemini import GeminiClient
from apps.api.domain.context import TenantContext
from baseline.alerts.models import AlertEvent
from baseline.b2_llm_no_tools import calculate_cost, load_pricing_config
from evals.graders.diagnosis import grade_diagnosis
from evals.graders.evidence import grade_evidence_support
from evals.graders.models import Diagnosis
from lab.scenarios.loader import load_scenario
from tests.test_investigator_loop_deterministic import (
    InMemoryRunRepo,
    InMemoryRunService,
    MockRunRecord,
    build_test_snapshot_environment,
)
from tools.read.backends.snapshot import SnapshotBackend
from tools.registry import _CALL_CACHE, ToolRegistry

load_dotenv()
logger = logging.getLogger("opspilot.evals.investigator_runner")


def get_git_sha() -> str:
    """Gets current git commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def get_scenario_alert(scenario_id: str, base_time: datetime) -> AlertEvent:
    """Returns the primary alert for the scenario."""
    if scenario_id == "S1":
        return AlertEvent(
            alert_id="alt_s1_001",
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
    elif scenario_id == "S2":
        return AlertEvent(
            alert_id="alt_s2_001",
            fired_at=base_time,
            service="inventory",
            rule_id="inventory_latency_p95_breach",
            metric="latency_p95",
            threshold=0.25,
            observed_value=0.85,
            window_seconds=300,
            severity="CRITICAL",
            status="firing",
        )
    elif scenario_id == "S3":
        return AlertEvent(
            alert_id="alt_s3_001",
            fired_at=base_time,
            service="inventory",
            rule_id="inventory_latency_p95_breach",
            metric="latency_p95",
            threshold=0.25,
            observed_value=0.45,
            window_seconds=300,
            severity="CRITICAL",
            status="firing",
        )
    elif scenario_id == "S4":
        return AlertEvent(
            alert_id="alt_s4_001",
            fired_at=base_time,
            service="inventory",
            rule_id="inventory_latency_p95_breach",
            metric="latency_p95",
            threshold=0.25,
            observed_value=0.45,
            window_seconds=300,
            severity="CRITICAL",
            status="firing",
        )
    elif scenario_id == "S5":
        return AlertEvent(
            alert_id="alt_s5_001",
            fired_at=base_time,
            service="orders",
            rule_id="orders_db_pool_surge",
            metric="db_pool_in_use",
            threshold=7.0,
            observed_value=8.0,
            window_seconds=300,
            severity="WARNING",
            status="firing",
        )
    else:
        raise ValueError(f"Unknown scenario ID: {scenario_id}")


SCENARIO_FILE_MAP = {
    "S1": "fixtures/scenarios/S1_pool_leak.yml",
    "S2": "fixtures/scenarios/S2_latency_regression.yml",
    "S3": "fixtures/scenarios/S3_dependency_timeout.yml",
    "S4": "fixtures/scenarios/S4_dependency_timeout_with_decoy.yml",
    "S5": "fixtures/scenarios/S5_false_alarm.yml",
}


def run_investigator_single_scenario(
    scenario_id: str,
    llm_client: GeminiClient,
    backend: SnapshotBackend,
    base_time: datetime,
    pricing_config: dict[str, Any],
    git_sha: str,
    prompt_hash: str,
    model_name: str,
) -> dict[str, Any]:
    """Runs the live investigator on a single scenario and returns complete trajectory record."""
    _CALL_CACHE.clear()

    scenario_file = SCENARIO_FILE_MAP[scenario_id]
    scenario = load_scenario(scenario_file)
    alert = get_scenario_alert(scenario_id, base_time)

    run_id = uuid4()
    tenant_id = uuid4()
    incident_id = uuid4()

    repo = InMemoryRunRepo()
    repo.runs[run_id] = MockRunRecord(
        run_id=run_id,
        tenant_id=tenant_id,
        status="investigating",
        version=1,
        max_calls=8,
    )
    run_service = InMemoryRunService(repo)
    tool_registry = ToolRegistry()

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=tool_registry,
        handoff_port=DefaultHandoffStub(run_service),  # type: ignore[arg-type]
        checkpointer=MemorySaver(),
        backend=backend,
    )

    initial_state = create_initial_state(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        alert=alert,
        max_calls=8,
    )

    start_wall = time.time()
    final_state = run_investigation_graph(graph, initial_state)
    end_wall = time.time()
    execution_wall_seconds = round(max(0.0, end_wall - start_wall), 3)

    # 1. Parse diagnosis draft
    diag_raw = final_state.get("draft_diagnosis") or {}
    try:
        diagnosis = Diagnosis.model_validate(diag_raw)
    except Exception:
        diagnosis = Diagnosis(
            root_cause_service=None,
            root_cause_category="unknown",
            mechanism="unknown",
            culprit_deploy_id=None,
            evidence_ids=[],
            confidence=0.0,
        )

    # 2. Grade diagnosis against scenario ground truth
    diag_grade = grade_diagnosis(scenario, diagnosis)

    # 3. Grade evidence support
    obs_list = repo.get_evidence_list(TenantContext(tenant_id=tenant_id), run_id)
    ev_grade = grade_evidence_support(
        diagnosis=diagnosis,
        evidence_store=obs_list,
        incident_window=(alert.fired_at - timedelta(seconds=600), alert.fired_at + timedelta(seconds=600)),
    )

    # 4. Token usage and cost
    total_tokens_consumed = final_state.get("total_tokens_consumed", 0)
    # Estimate input vs output split (~85% input, 15% output)
    est_input_tokens = int(total_tokens_consumed * 0.85)
    est_output_tokens = total_tokens_consumed - est_input_tokens

    cost_usd = calculate_cost(
        model_name=model_name,
        input_tokens=est_input_tokens,
        output_tokens=est_output_tokens,
        pricing_config=pricing_config,
    )

    # 5. Extract events and provider errors
    raw_events = [e.model_dump(mode="json") if hasattr(e, "model_dump") else dict(e) for e in repo.events]
    provider_errors = [
        e for e in raw_events if e.get("event_type") in ("provider_error_escalated", "provider_error")
    ]

    tool_trace = final_state.get("tool_trace", [])
    tools_called = [t.get("tool") for t in tool_trace]

    diag_outcome_str = (
        "correct" if diag_grade.correct
        else "partial" if diag_grade.partial
        else "abstained" if diag_grade.abstained
        else "wrong"
    )

    trajectory = {
        "scenario_id": scenario_id,
        "trial": 1,
        "backend": "snapshot",
        "alert": alert.model_dump(mode="json"),
        "final_status": final_state.get("status"),
        "termination_reason": final_state.get("termination_reason"),
        "step_count": final_state.get("step_count", 0),
        "remaining_calls": final_state.get("remaining_calls", 8),
        "tool_trace": tool_trace,
        "tools_called": tools_called,
        "evidence_ids": final_state.get("evidence_ids", []),
        "diagnosis": diagnosis.model_dump(mode="json"),
        "diagnosis_outcome": diag_outcome_str,
        "grades": {
            "diagnosis": diag_grade.model_dump(mode="json"),
            "evidence": ev_grade.model_dump(mode="json"),
        },
        "tokens": {
            "input_tokens_est": est_input_tokens,
            "output_tokens_est": est_output_tokens,
            "total_tokens": total_tokens_consumed,
        },
        "latency_ms": round(execution_wall_seconds * 1000.0, 1),
        "execution_wall_seconds": execution_wall_seconds,
        "cost_estimate_usd": cost_usd,
        "pricing_source": {
            "checked_on": pricing_config.get("checked_on", "2026-11-24"),
            "unverified": pricing_config.get("unverified", True),
        },
        "provider_errors": provider_errors,
        "prompt_hash": prompt_hash,
        "model_settings": {
            "model": model_name,
            "temperature": 0.0,
            "max_output_tokens": 1024,
        },
        "git_sha": git_sha,
        "messages_summary": final_state.get("messages_summary", ""),
        "last_error": final_state.get("last_error"),
        "events": raw_events,
    }

    return trajectory


def main() -> None:
    api_key_str = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key_str:
        print("ERROR: GEMINI_API_KEY is not set. Stopping.")
        return

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
    print("=== OPSPILOT INVESTIGATOR EVALUATION ===")
    print(f"Model: {model_name}")
    print("Backend: SnapshotBackend (isolated snapshot telemetry fixtures)")

    # 1. Prompt and hash
    sys_prompt = load_system_prompt()
    prompt_hash = compute_prompt_hash(sys_prompt)
    print(f"Prompt Hash: {prompt_hash}")
    assert prompt_hash == EXPECTED_PROMPT_V1_HASH, f"Prompt hash mismatch: {prompt_hash} != {EXPECTED_PROMPT_V1_HASH}"

    git_sha = get_git_sha()
    print(f"Git SHA: {git_sha}")

    pricing_config = load_pricing_config()
    print(f"Pricing checked on: {pricing_config.get('checked_on')}")

    # 2. Output directory
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"evals/results/investigator/{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output Directory: {out_dir}\n")

    # 3. Setup snapshot backend
    tmp_snapshot_dir = Path(f"evals/results/investigator/{timestamp}/_fixtures")
    build_test_snapshot_environment(tmp_snapshot_dir)
    backend = SnapshotBackend(tmp_snapshot_dir)

    base_time = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    llm_client = GeminiClient(
        api_key=SecretStr(api_key_str),
        model_name=model_name,
    )

    results = []
    scenarios = ["S1", "S2", "S3", "S4", "S5"]

    for scn in scenarios:
        print(f"--> Running Scenario {scn}...")
        t0 = time.time()
        traj = run_investigator_single_scenario(
            scenario_id=scn,
            llm_client=llm_client,
            backend=backend,
            base_time=base_time,
            pricing_config=pricing_config,
            git_sha=git_sha,
            prompt_hash=prompt_hash,
            model_name=model_name,
        )
        elapsed = round(time.time() - t0, 2)

        # Write individual scenario file
        scn_file = out_dir / f"{scn}.json"
        scn_file.write_text(json.dumps(traj, indent=2, sort_keys=True, default=str), encoding="utf-8")

        outcome = traj["diagnosis_outcome"]
        tools = traj["tools_called"]
        tokens = traj["tokens"]["total_tokens"]
        diag = traj["diagnosis"]
        print(f"    Completed in {elapsed}s | Outcome: {outcome} | Tools: {tools} | Tokens: {tokens}")
        print(f"    Category: {diag.get('root_cause_category')} | Mechanism: {diag.get('mechanism')} | Service: {diag.get('root_cause_service')}")
        results.append(traj)

    # 4. Summary file
    summary_file = out_dir / "summary.json"
    summary_file.write_text(json.dumps(results, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"\nAll 5 scenarios completed. Summary written to {summary_file}")


if __name__ == "__main__":
    main()
