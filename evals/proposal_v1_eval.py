"""evals/proposal_v1_eval.py

Live end-to-end evaluation of Investigator v2 -> Proposal v1 (propose_node + Policy Validator)
across scenarios S1-S5 (trials=3 each = 15 runs total) using SnapshotBackend and SnapshotTargetReader.

Saves raw trajectories, proposals, rejections, tokens, latency, cost, provider errors,
prompt hashes, policy_version, and git SHA under:
  evals/results/proposal_v1/<timestamp>/
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
    EXPECTED_PROMPT_V2_HASH,
    PROMPT_HASHES_FILE,
    SYSTEM_PROMPT_V2_PATH,
    compute_prompt_hash,
    load_system_prompt,
)
from agent.graph.graph import build_investigator_graph, run_investigation_graph
from agent.graph.nodes.propose import EXPECTED_PROPOSAL_V1_HASH, load_proposal_prompt
from agent.graph.state import create_initial_state
from agent.llm.gemini import GeminiClient
from agent.llm.interface import TokenBudgetGuard
from agent.policy import EXPECTED_POLICY_VERSION, compute_policy_version
from apps.api.domain.context import TenantContext
from baseline.b2_llm_no_tools import calculate_cost, load_pricing_config
from evals.graders.diagnosis import grade_diagnosis
from lab.scenarios.loader import load_scenario
from shared.actions import grade_action
from shared.plans import ActionPlan
from tests.test_v2_loop_deterministic import (
    _build_snapshot_fixture,
    _InMemoryRepo,
    _InMemoryRunService,
    _make_alert,
    _MockRunRecord,
)
from tools.read.backends.snapshot import SnapshotBackend
from tools.read.target_state import SnapshotTargetReader
from tools.registry import _CALL_CACHE, ToolRegistry

load_dotenv()
logger = logging.getLogger("opspilot.evals.proposal_v1_eval")

SCENARIO_FILE_MAP = {
    "S1": Path("fixtures/scenarios/S1_pool_leak.yml"),
    "S2": Path("fixtures/scenarios/S2_latency_regression.yml"),
    "S3": Path("fixtures/scenarios/S3_dependency_timeout.yml"),
    "S4": Path("fixtures/scenarios/S4_dependency_timeout_with_decoy.yml"),
    "S5": Path("fixtures/scenarios/S5_false_alarm.yml"),
}


def get_git_sha() -> str:
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def verify_frozen_artifacts() -> tuple[str, str, str]:
    committed = json.loads(PROMPT_HASHES_FILE.read_text(encoding="utf-8"))
    h_v2 = compute_prompt_hash(load_system_prompt(SYSTEM_PROMPT_V2_PATH, version="v2"))
    assert h_v2 == EXPECTED_PROMPT_V2_HASH == committed["investigator_system_v2.md"], "v2 prompt hash mismatch"
    load_proposal_prompt()
    assert committed["proposal_v1.md"] == EXPECTED_PROPOSAL_V1_HASH, "proposal_v1 hash mismatch"
    pv = compute_policy_version()
    assert pv == EXPECTED_POLICY_VERSION, f"policy_version mismatch: {pv} != {EXPECTED_POLICY_VERSION}"
    return h_v2, EXPECTED_PROPOSAL_V1_HASH, pv


def _expected_action_for_eval(scenario_id: str, scenario: Any) -> dict[str, Any]:
    if scenario_id in ("S3", "S4"):
        # Per approved proposal_design.md Decision #3: dependency_failure targets affected service 'inventory'
        return {"action": "restart_service", "service": "inventory"}
    exp = scenario.ground_truth.expected_action
    return {"action": exp.action, "service": exp.service}


def run_single_trial(
    *,
    scenario_id: str,
    trial: int,
    api_key: SecretStr,
    model_name: str,
    backend: SnapshotBackend,
    target_reader: SnapshotTargetReader,
    base_time: datetime,
    pricing_config: dict[str, Any],
    git_sha: str,
    v2_hash: str,
    proposal_hash: str,
    policy_version: str,
    max_tokens_per_run: int = 50000,
) -> dict[str, Any]:
    _CALL_CACHE.clear()
    scenario = load_scenario(SCENARIO_FILE_MAP[scenario_id])
    alert = _make_alert(scenario_id, base_time)

    run_id = uuid4()
    tenant_id = uuid4()
    incident_id = uuid4()

    repo = _InMemoryRepo()
    repo.runs[run_id] = _MockRunRecord(run_id, tenant_id, status="investigating", version=1)
    run_service = _InMemoryRunService(repo)
    persisted_plans: list[ActionPlan] = []

    def _plan_persister(_ctx: TenantContext, plan: ActionPlan, _sup: str | None) -> None:
        persisted_plans.append(plan)

    budget_guard = TokenBudgetGuard(max_tokens=max_tokens_per_run)
    llm_client = GeminiClient(
        api_key=api_key,
        model_name=model_name,
        budget_guard=budget_guard,
    )

    graph = build_investigator_graph(
        llm_client=llm_client,
        run_service=run_service,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        handoff_port=None,
        checkpointer=MemorySaver(),
        backend=backend,
        target_reader=target_reader,
        plan_persister=_plan_persister,
        db_clock_fn=lambda: datetime.now(timezone.utc),
    )

    init_state = create_initial_state(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        incident_id=str(incident_id),
        alert=alert,
        schema_version="v2",
    )

    t0 = time.perf_counter()
    error_message: str | None = None
    try:
        final_state = dict(run_investigation_graph(graph, init_state, recursion_limit=40))
    except Exception as exc:
        final_state = dict(init_state)
        final_state["status"] = "failed"
        final_state["termination_reason"] = "graph_exception"
        error_message = str(exc)
    wall_s = round(time.perf_counter() - t0, 3)

    events = list(repo.events)
    proposal_attempts = [e for e in events if e.get("event_type") == "proposal_attempt"]
    rejection_codes: list[str] = []
    for pa in proposal_attempts:
        for code in (pa.get("payload") or {}).get("violation_codes", []):
            rejection_codes.append(str(code))

    diag_raw = final_state.get("draft_diagnosis")
    diag_grade = None
    if diag_raw:
        try:
            diag_grade = grade_diagnosis(scenario, diag_raw).model_dump()
        except Exception:
            diag_grade = None

    # Action correctness
    actual_action: dict[str, Any]
    if persisted_plans:
        p0 = persisted_plans[-1]
        actual_action = {"action": p0.action.value, "service": p0.service}
    elif scenario_id == "S5" and final_state.get("status") == "completed":
        actual_action = {"action": "no_action"}
    else:
        actual_action = {"action": "escalate"}

    exp_action = _expected_action_for_eval(scenario_id, scenario)
    action_grade = grade_action(exp_action, actual_action).model_dump()

    total_tokens = int(final_state.get("total_tokens_consumed", 0))
    in_tok = int(total_tokens * 0.8)
    out_tok = total_tokens - in_tok
    est_cost = calculate_cost(model_name, in_tok, out_tok, pricing_config)

    term_reason = str(final_state.get("termination_reason") or "unknown")
    is_provider_error = term_reason in ("provider_error_exhausted", "invalid_step_cap_reached") or any(
        e.get("event_type") in ("provider_error", "invalid_steps_escalated") for e in events
    )

    return {
        "scenario_id": scenario_id,
        "trial": trial,
        "run_id": str(run_id),
        "git_sha": git_sha,
        "model": model_name,
        "v2_prompt_hash": v2_hash,
        "proposal_prompt_hash": proposal_hash,
        "policy_version": policy_version,
        "final_status": final_state.get("status"),
        "termination_reason": term_reason,
        "is_provider_error": is_provider_error,
        "provider_error_details": final_state.get("last_error") if is_provider_error else None,
        "error_message": error_message,
        "diagnosis": diag_raw,
        "diagnosis_grade": diag_grade,
        "persisted_plans": [p.model_dump(mode="json") for p in persisted_plans],
        "proposal_attempts": [e.get("payload") for e in proposal_attempts],
        "rejection_codes": rejection_codes,
        "expected_action": exp_action,
        "actual_action": actual_action,
        "action_grade": action_grade,
        "tool_calls_used": len(final_state.get("tool_trace") or []),
        "total_tokens": total_tokens,
        "estimated_cost_usd": est_cost,
        "wall_time_seconds": wall_s,
        "events": events,
    }


def main() -> None:
    raw_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not raw_key:
        raise RuntimeError("GEMINI_API_KEY is not set; stopping without faking results.")

    api_key = SecretStr(raw_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip()
    git_sha = get_git_sha()
    v2_hash, proposal_hash, policy_version = verify_frozen_artifacts()
    pricing_config = load_pricing_config()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path("evals/results/proposal_v1") / ts
    fixtures_dir = out_dir / "_fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    results: list[dict[str, Any]] = []

    for scenario_id in ("S1", "S2", "S3", "S4", "S5"):
        for trial in (1, 2, 3):
            scen_dir = fixtures_dir / f"{scenario_id}_t{trial}"
            _build_snapshot_fixture(scen_dir, base_time, scenario_id)
            backend = SnapshotBackend(scen_dir)
            reader = SnapshotTargetReader(
                snapshot_dir=scen_dir,
                snapshot_read_at=base_time + timedelta(minutes=5),
                max_age_seconds=3600,
            )
            res = run_single_trial(
                scenario_id=scenario_id,
                trial=trial,
                api_key=api_key,
                model_name=model_name,
                backend=backend,
                target_reader=reader,
                base_time=base_time,
                pricing_config=pricing_config,
                git_sha=git_sha,
                v2_hash=v2_hash,
                proposal_hash=proposal_hash,
                policy_version=policy_version,
            )
            results.append(res)
            (out_dir / f"{scenario_id}_t{trial}.json").write_text(
                json.dumps(res, indent=2, default=str), encoding="utf-8"
            )
            print(
                f"{scenario_id} t{trial}: status={res['final_status']} reason={res['termination_reason']} "
                f"action={res['actual_action']} correct={res['action_grade']['correct']} "
                f"provider_err={res['is_provider_error']} tokens={res['total_tokens']} wall={res['wall_time_seconds']}s",
                flush=True,
            )
            time.sleep(2)

    summary = {
        "timestamp": ts,
        "git_sha": git_sha,
        "model": model_name,
        "v2_prompt_hash": v2_hash,
        "proposal_prompt_hash": proposal_hash,
        "policy_version": policy_version,
        "pricing_checked_on": pricing_config.get("checked_on"),
        "trials": len(results),
        "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"SAVED_TO={out_dir}")


if __name__ == "__main__":
    main()
