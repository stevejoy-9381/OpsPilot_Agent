"""evals/investigator_v2_compare.py

Live comparison harness running OpsPilot Investigator v1 (flat prompt, no ledger)
vs v2 (hypothesis ledger + contrary-evidence gate) across scenarios S1-S5 (trials=3 each)
with identical model, settings, spending cap, and SnapshotBackend fixtures.

Saves raw trajectories, events, tokens, latency, cost estimates, provider errors,
gate rejections, model settings, prompt hashes, and git SHA under:
  evals/results/investigator_v2/<timestamp>/
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

from agent.graph.context import (
    EXPECTED_PROMPT_V1_HASH,
    EXPECTED_PROMPT_V2_HASH,
    PROMPT_HASHES_FILE,
    SYSTEM_PROMPT_V1_PATH,
    SYSTEM_PROMPT_V2_PATH,
    compute_prompt_hash,
    load_system_prompt,
)
from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.handoff import DefaultHandoffStub
from agent.graph.nodes.finalize import ALLOWED_TOOL_SOURCES
from agent.graph.state import create_initial_state
from agent.llm.gemini import GeminiClient
from agent.llm.interface import TokenBudgetGuard
from apps.api.domain.context import TenantContext
from baseline.b2_llm_no_tools import calculate_cost, load_pricing_config
from evals.graders.diagnosis import grade_diagnosis
from evals.graders.evidence import grade_evidence_support
from evals.graders.models import Diagnosis, EvidenceItem, EvidenceStore
from lab.scenarios.loader import load_scenario
from tests.test_v2_loop_deterministic import (
    _build_snapshot_fixture,
    _InMemoryRepo,
    _InMemoryRunService,
    _make_alert,
    _MockRunRecord,
)
from tools.read.backends.snapshot import SnapshotBackend
from tools.registry import _CALL_CACHE, ToolRegistry

load_dotenv()
logger = logging.getLogger("opspilot.evals.investigator_v2_compare")

SCENARIO_FILE_MAP = {
    "S1": Path("fixtures/scenarios/S1_pool_leak.yml"),
    "S2": Path("fixtures/scenarios/S2_latency_regression.yml"),
    "S3": Path("fixtures/scenarios/S3_dependency_timeout.yml"),
    "S4": Path("fixtures/scenarios/S4_dependency_timeout_with_decoy.yml"),
    "S5": Path("fixtures/scenarios/S5_false_alarm.yml"),
}


def get_git_sha() -> str:
    """Gets current git commit SHA."""
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def verify_frozen_artifacts() -> tuple[str, str]:
    """Confirms prompt v1/v2 hashes match committed values and gate/cap tables are unmodified."""
    committed = json.loads(PROMPT_HASHES_FILE.read_text(encoding="utf-8"))
    h1 = compute_prompt_hash(load_system_prompt(SYSTEM_PROMPT_V1_PATH, version="v1"))
    h2 = compute_prompt_hash(load_system_prompt(SYSTEM_PROMPT_V2_PATH, version="v2"))
    assert h1 == EXPECTED_PROMPT_V1_HASH == committed["investigator_system_v1.md"], "v1 prompt hash mismatch"
    assert h2 == EXPECTED_PROMPT_V2_HASH == committed["investigator_system_v2.md"], "v2 prompt hash mismatch"

    git_check = subprocess.run(
        ["git", "status", "--porcelain", "agent/prompts/", "agent/graph/gate.py", "agent/graph/confidence_caps.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not git_check.stdout.strip(), f"Frozen artifacts have uncommitted changes:\n{git_check.stdout}"
    return h1, h2


def run_single_trial(
    *,
    version: str,
    scenario_id: str,
    trial: int,
    api_key: SecretStr,
    model_name: str,
    backend: SnapshotBackend,
    base_time: datetime,
    pricing_config: dict[str, Any],
    git_sha: str,
    prompt_hash: str,
    max_tokens_per_run: int = 50000,
) -> dict[str, Any]:
    """Executes one live trial of v1 or v2 on a scenario."""
    _CALL_CACHE.clear()
    scenario = load_scenario(SCENARIO_FILE_MAP[scenario_id])
    alert = _make_alert(scenario_id, base_time)

    run_id = uuid4()
    tenant_id = uuid4()
    incident_id = uuid4()

    repo = _InMemoryRepo()
    repo.runs[run_id] = _MockRunRecord(run_id, tenant_id, status="investigating", version=1)
    run_service = _InMemoryRunService(repo)
    tool_registry = ToolRegistry()

    llm_client = GeminiClient(
        api_key=api_key,
        model_name=model_name,
        budget_guard=TokenBudgetGuard(max_tokens=max_tokens_per_run),
    )

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
        schema_version=version,
    )

    t0 = time.time()
    final_state = run_investigation_graph(graph, initial_state)
    wall_s = round(max(0.0, time.time() - t0), 3)

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

    diag_grade = grade_diagnosis(scenario, diagnosis)

    obs_list = repo.get_evidence_list(TenantContext(tenant_id=tenant_id), run_id)
    ev_store = EvidenceStore(
        [
            EvidenceItem(evidence_id=o.evidence_id, timestamp=o.captured_at, source=o.tool, data=o.payload or {})
            for o in obs_list
        ]
    )
    ev_grade = grade_evidence_support(
        diagnosis=diagnosis,
        evidence_store=ev_store,
        incident_window=(base_time - timedelta(minutes=15), base_time + timedelta(minutes=15)),
        allowed_tools=ALLOWED_TOOL_SOURCES,
    )

    total_tokens = int(final_state.get("total_tokens_consumed", 0))
    est_in = int(total_tokens * 0.85)
    est_out = total_tokens - est_in
    cost_usd = calculate_cost(
        model_name=model_name,
        input_tokens=est_in,
        output_tokens=est_out,
        pricing_config=pricing_config,
    )

    raw_events = [dict(e) for e in repo.events]
    provider_errors = [
        e for e in raw_events if e.get("event_type") in ("provider_error_escalated", "provider_error")
    ]
    gate_rejections = [
        e for e in raw_events if e.get("event_type") in ("gate_rejection_emitted", "gate_rejection_cap_reached")
    ]

    tool_trace = final_state.get("tool_trace", [])
    ledger = final_state.get("hypotheses_ledger", {})
    term_reason = final_state.get("termination_reason")

    if provider_errors or term_reason == "provider_error_exhausted":
        outcome_label = "provider_error"
    elif diag_grade.correct:
        outcome_label = "correct"
    elif diag_grade.partial:
        outcome_label = "partial"
    elif diag_grade.abstained:
        outcome_label = "abstained"
    else:
        outcome_label = "wrong"

    # Overconfident: positive diagnosis (not unknown) that is wrong/partial OR confidence > 0.70 when unsupported
    is_overconfident = (
        not provider_errors
        and diagnosis.root_cause_category != "unknown"
        and (not diag_grade.correct or not ev_grade.supported)
    )
    # Overcautious: abstained (unknown) on a diagnosable scenario (S1-S4) when no provider error occurred
    is_overcautious = (
        not provider_errors
        and diag_grade.abstained
        and scenario_id in ("S1", "S2", "S3", "S4")
    )

    return {
        "version": version,
        "scenario_id": scenario_id,
        "trial": trial,
        "backend": "snapshot",
        "alert": alert.model_dump(mode="json"),
        "final_status": final_state.get("status"),
        "termination_reason": term_reason,
        "step_count": final_state.get("step_count", 0),
        "tool_calls_used": len(tool_trace),
        "remaining_calls": final_state.get("remaining_calls", 8),
        "hypotheses_count": len(ledger),
        "hypotheses_ledger": ledger,
        "gate_rejection_count": final_state.get("gate_rejection_count", 0),
        "gate_rejections": gate_rejections,
        "tool_trace": tool_trace,
        "evidence_ids": final_state.get("evidence_ids", []),
        "diagnosis": diagnosis.model_dump(mode="json"),
        "diagnosis_outcome": outcome_label,
        "overconfident": is_overconfident,
        "overcautious": is_overcautious,
        "grades": {
            "diagnosis": diag_grade.model_dump(mode="json"),
            "evidence": ev_grade.model_dump(mode="json"),
        },
        "tokens": {
            "input_tokens_est": est_in,
            "output_tokens_est": est_out,
            "total_tokens": total_tokens,
        },
        "latency_ms": round(wall_s * 1000.0, 1),
        "execution_wall_seconds": wall_s,
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
            "spending_cap_tokens_per_run": max_tokens_per_run,
        },
        "git_sha": git_sha,
        "messages_summary": final_state.get("messages_summary", ""),
        "last_error": final_state.get("last_error"),
        "events": raw_events,
    }


def main() -> None:
    api_key_str = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key_str:
        print("ERROR: GEMINI_API_KEY is not set. Stopping.")
        return

    h1, h2 = verify_frozen_artifacts()
    git_sha = get_git_sha()
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
    pricing_config = load_pricing_config()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"evals/results/investigator_v2/{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== OPSPILOT LIVE COMPARISON: PROMPT V1 vs V2 ===")
    print(f"Model: {model_name} | Git SHA: {git_sha}")
    print(f"V1 Hash: {h1}")
    print(f"V2 Hash: {h2}")
    print(f"Pricing checked on: {pricing_config.get('checked_on')}")
    print(f"Output Dir: {out_dir}\n")

    api_key = SecretStr(api_key_str)
    all_results: list[dict[str, Any]] = []
    scenarios = ["S1", "S2", "S3", "S4", "S5"]
    trials = 3

    for version, p_hash in [("v1", h1), ("v2", h2)]:
        for scn in scenarios:
            for trial in range(1, trials + 1):
                base_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                snap_dir = out_dir / "_fixtures" / f"{version}_{scn}_t{trial}"
                backend = _build_snapshot_fixture(snap_dir, base_time, scn)

                print(f"--> [{version}] {scn} trial {trial}/{trials}...", flush=True)
                traj = run_single_trial(
                    version=version,
                    scenario_id=scn,
                    trial=trial,
                    api_key=api_key,
                    model_name=model_name,
                    backend=backend,
                    base_time=base_time,
                    pricing_config=pricing_config,
                    git_sha=git_sha,
                    prompt_hash=p_hash,
                )
                out_file = out_dir / f"{version}_{scn}_trial{trial}.json"
                out_file.write_text(json.dumps(traj, indent=2, sort_keys=True, default=str), encoding="utf-8")
                all_results.append(traj)
                print(
                    f"    status={traj['final_status']} outcome={traj['diagnosis_outcome']} "
                    f"calls={traj['tool_calls_used']} tokens={traj['tokens']['total_tokens']} "
                    f"provider_errs={len(traj['provider_errors'])} gate_rej={traj['gate_rejection_count']}",
                    flush=True,
                )

    summary_file = out_dir / "summary.json"
    summary_file.write_text(json.dumps(all_results, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"\nCompleted {len(all_results)} trials. Summary written to {summary_file}")


if __name__ == "__main__":
    main()
