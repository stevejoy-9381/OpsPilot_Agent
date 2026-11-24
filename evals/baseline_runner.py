"""Baseline evaluation runner across B0, B1, and B2 variants.

CLI usage:
    python -m evals.baseline_runner --variants b0,b1,b2 --scenarios all|S1,S3 --trials 1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.llm.fake import FakeLLMClient
from agent.llm.interface import LLMClient
from baseline.alerts.evaluator import evaluate as evaluate_alerts
from baseline.alerts.evaluator import load_rules
from baseline.alerts.models import AlertEvent
from baseline.b0_alert_only import run_b0
from baseline.b1_runbook import DEFAULT_RUNBOOK_PATH, run_b1
from baseline.b2_llm_no_tools import (
    DEFAULT_MODEL,
    DEFAULT_PROMPT_PATH,
    B2LLMResponse,
    run_b2,
)
from baseline.models import BaselineResult
from evals.graders.diagnosis import grade_diagnosis
from lab.scenarios.loader import load_all_scenarios
from lab.scenarios.models import Scenario
from shared.actions import expected_action_from_truth, grade_action
from tools.read.backends.snapshot import SnapshotBackend

logger = logging.getLogger("opspilot.evals.baseline_runner")

HARNESS_VERSION = "1.0.0"


def compute_sha256(path: Path) -> str:
    """Computes SHA-256 hash of a file."""
    if not path.is_file():
        return "unknown"
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def build_scenario_snapshot_backend(
    base_dir: Path,
    scenario_id: str,
    base_time: datetime,
) -> SnapshotBackend:
    """Sets up isolated snapshot metrics & deploy state for a scenario."""
    from scripts.run_scenarios_b0_b1 import setup_scenario_fixture

    return setup_scenario_fixture(base_dir, scenario_id, base_time)


class BaselineRunner:
    """Evaluates baseline variants (B0, B1, B2) across scenarios."""

    def __init__(
        self,
        variants: list[str],
        scenario_ids: list[str] | None = None,
        trials: int = 1,
        out_dir: Path | None = None,
        offline: bool = False,
        client: LLMClient | None = None,
        base_time: datetime | None = None,
    ) -> None:
        self.variants = [v.lower().strip() for v in variants]
        self.scenario_ids = scenario_ids
        self.trials = max(1, trials)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.out_dir = out_dir or Path(f"evals/results/baseline/{timestamp}")
        self.offline = offline
        self.client = client
        self.base_time = base_time or datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
        self.git_sha = get_git_sha()
        self.runbook_hash = compute_sha256(DEFAULT_RUNBOOK_PATH)
        self.prompt_hash = compute_sha256(DEFAULT_PROMPT_PATH)
        self.alert_rules = load_rules()

    def load_scenarios(self) -> list[Scenario]:
        """Loads and filters target scenarios."""
        all_scenarios = load_all_scenarios()
        if not self.scenario_ids or "all" in [s.lower() for s in self.scenario_ids]:
            return all_scenarios
        target_ids = {s.upper().strip() for s in self.scenario_ids}
        return [s for s in all_scenarios if s.scenario_id.upper() in target_ids]

    async def get_scenario_alert(
        self,
        scenario: Scenario,
        backend: SnapshotBackend,
    ) -> AlertEvent:
        """Evaluates rules over backend metrics up to alert window and returns trigger alert."""
        from datetime import timedelta

        window = (self.base_time, self.base_time + timedelta(minutes=10))
        alerts = await evaluate_alerts(self.alert_rules, backend, window)
        if not alerts:
            raise RuntimeError(f"No alert fired for scenario {scenario.scenario_id}")

        # Match primary alert per design
        if scenario.scenario_id in ("S1", "S5"):
            matches = [a for a in alerts if a.rule_id == "orders_db_pool_saturation"]
            if matches:
                return matches[0]
        else:
            matches = [a for a in alerts if a.rule_id == "inventory_latency_p95_high"]
            if matches:
                return matches[0]

        return alerts[0]

    def _get_b2_client(self) -> LLMClient:
        """Returns the appropriate LLM client for B2 execution."""
        if self.client is not None:
            return self.client

        if self.offline:
            # Deterministic FakeLLMClient for offline evaluation/testing
            # Returns an unknown/escalate diagnosis & action to follow baseline instructions
            from evals.graders.models import Diagnosis
            from shared.actions import ActionType, RecommendedAction

            diag = Diagnosis(
                root_cause_service=None,
                root_cause_category="unknown",
                mechanism="unknown",
                culprit_deploy_id=None,
                evidence_ids=[],
                confidence=0.15,
            )
            act = RecommendedAction(
                action=ActionType.ESCALATE,
                service=None,
                rationale_ids=[],
                notes="Offline evaluation: no live telemetry available to verify root cause.",
            )
            resp = B2LLMResponse(
                diagnosis=diag,
                action=act,
                confidence=0.15,
                uncertainty_note="Offline test mode with FakeLLMClient.",
            )
            responses = [resp] * 100
            return FakeLLMClient(responses=responses)

        # Live Gemini Client
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set in environment. Cannot run B2 in live mode."
            )
        from pydantic import SecretStr

        from agent.llm.gemini import GeminiClient

        return GeminiClient(
            api_key=SecretStr(api_key),
            model_name=DEFAULT_MODEL,
        )

    async def run_single(
        self,
        scenario: Scenario,
        variant: str,
        trial: int,
        alert: AlertEvent,
        backend: SnapshotBackend,
    ) -> dict[str, Any]:
        """Runs a single variant on a scenario and returns graded result dictionary."""
        # Record wall clock completion time from alert.fired_at to recommendation produced
        start_wall = time.time()

        res: BaselineResult
        if variant == "b0":
            res = run_b0(alert, scenario_id=scenario.scenario_id)
        elif variant == "b1":
            res = await run_b1(
                alert,
                backend=backend,
                scenario_id=scenario.scenario_id,
            )
        elif variant == "b2":
            llm_client = self._get_b2_client()
            res = run_b2(
                alert,
                client=llm_client,
                scenario_id=scenario.scenario_id,
                trial=trial,
            )
        else:
            raise ValueError(f"Unknown variant {variant}")

        end_wall = time.time()
        execution_wall_seconds = max(0.0, end_wall - start_wall)

        # Completion time relative to alert fired_at
        now_utc = datetime.now(timezone.utc)
        alert_fired = alert.fired_at
        if alert_fired.tzinfo is None:
            alert_fired = alert_fired.replace(tzinfo=timezone.utc)
        completion_time_seconds = max(0.0, (now_utc - alert_fired).total_seconds())

        # Grade diagnosis against scenario ground truth
        diag_grade = grade_diagnosis(scenario, res.diagnosis)

        # Grade action against scenario expected action
        expected_action = expected_action_from_truth(scenario.ground_truth.expected_action)
        act_grade = grade_action(expected_action, res.action)

        # Determine diagnosis outcome string
        is_wrong = not (diag_grade.correct or diag_grade.partial or diag_grade.abstained)
        diag_str = (
            "correct" if diag_grade.correct
            else "partial" if diag_grade.partial
            else "abstained" if diag_grade.abstained
            else "wrong"
        )

        model_settings = {}
        if variant == "b2":
            model_settings = {
                "model": DEFAULT_MODEL,
                "temperature": 0.0,
                "max_output_tokens": 1024,
            }

        latency = (
            res.latency_ms
            if res.latency_ms > 0
            else round(execution_wall_seconds * 1000, 2)
        )

        # Build schema-compliant per-run dictionary
        run_record: dict[str, Any] = {
            "scenario_id": scenario.scenario_id,
            "variant": variant,
            "trial": trial,
            "alert": alert.model_dump(mode="json"),
            "diagnosis": res.diagnosis.model_dump(mode="json"),
            "action": res.action.model_dump(mode="json"),
            "grades": {
                "diagnosis": {
                    "grade": diag_str,
                    "is_correct": diag_grade.correct,
                    "is_partial": diag_grade.partial,
                    "is_wrong": is_wrong,
                    "is_abstained": diag_grade.abstained,
                    "reasons": diag_grade.reasons,
                },
                "action": {
                    "correct": act_grade.correct,
                    "unnecessary": act_grade.unnecessary,
                    "wrong_target": act_grade.wrong_target,
                    "missing": act_grade.missing,
                    "reasons": act_grade.reasons,
                },
            },
            "tool_calls": res.tool_calls,
            "tokens": {
                "tokens_in": res.tokens_in,
                "tokens_out": res.tokens_out,
                "total": res.tokens_in + res.tokens_out,
            },
            "latency_ms": latency,
            "completion_time_seconds": round(completion_time_seconds, 2),
            "execution_wall_seconds": round(execution_wall_seconds, 3),
            "cost_estimate": res.cost_usd,
            "errors": res.error_type,
            "prompt_hash": res.prompt_hash or self.prompt_hash if variant == "b2" else None,
            "runbook_hash": self.runbook_hash if variant == "b1" else None,
            "model_settings": model_settings,
            "git_sha": self.git_sha,
            "harness_version": HARNESS_VERSION,
        }
        return run_record

    async def run(self) -> list[dict[str, Any]]:
        """Executes full evaluation matrix across variants, scenarios, and trials."""
        scenarios = self.load_scenarios()
        if not scenarios:
            raise RuntimeError(f"No scenarios found matching: {self.scenario_ids}")

        self.out_dir.mkdir(parents=True, exist_ok=True)
        all_results: list[dict[str, Any]] = []

        for scenario in scenarios:
            logger.info("Evaluating Scenario: %s (%s)", scenario.scenario_id, scenario.family)

            # Setup isolated scenario world
            with tempfile.TemporaryDirectory() as tmp_dir:
                backend = build_scenario_snapshot_backend(
                    Path(tmp_dir),
                    scenario.scenario_id,
                    self.base_time,
                )
                # Deterministic alert event shared across all variants
                alert = await self.get_scenario_alert(scenario, backend)

                for variant in self.variants:
                    for trial in range(1, self.trials + 1):
                        record = await self.run_single(
                            scenario=scenario,
                            variant=variant,
                            trial=trial,
                            alert=alert,
                            backend=backend,
                        )
                        all_results.append(record)

                        # Write individual run JSON file
                        run_file = (
                            self.out_dir
                            / f"{scenario.scenario_id}_{variant}_t{trial}.json"
                        )
                        run_file.write_text(json.dumps(record, indent=2), encoding="utf-8")

        # Save aggregated raw results
        summary_file = self.out_dir / "summary.json"
        summary_file.write_text(json.dumps(all_results, indent=2), encoding="utf-8")

        return all_results


def aggregate_results(
    results: list[dict[str, Any]],
    scenarios: list[Scenario],
) -> str:
    """Formats aggregated metrics with strict numerators/denominators (no bare percentages)."""
    scenario_family_map = {s.scenario_id: s.family for s in scenarios}
    variants = sorted(list({r["variant"] for r in results}))
    families = sorted(
        list({scenario_family_map.get(r["scenario_id"], "unknown") for r in results})
    )

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("BASELINE EVALUATION AGGREGATE RESULTS")
    lines.append("=" * 80)

    # 1. Per-Variant Summary
    lines.append("\n[1] PER-VARIANT SUMMARY")
    lines.append("-" * 80)
    lines.append(
        f"{'Variant':<8} | {'Diag Correct':<14} | {'Diag Abstained':<14} | "
        f"{'Action Correct':<14} | {'Unnecessary':<12} | {'Errors':<8}"
    )
    lines.append("-" * 80)

    for v in variants:
        v_runs = [r for r in results if r["variant"] == v]
        tot = len(v_runs)
        if tot == 0:
            continue

        diag_corr = sum(1 for r in v_runs if r["grades"]["diagnosis"]["is_correct"])
        diag_abst = sum(1 for r in v_runs if r["grades"]["diagnosis"]["is_abstained"])
        act_corr = sum(1 for r in v_runs if r["grades"]["action"]["correct"])
        act_unnec = sum(1 for r in v_runs if r["grades"]["action"]["unnecessary"])
        errors = sum(1 for r in v_runs if r.get("errors") is not None)

        lines.append(
            f"{v.upper():<8} | {f'{diag_corr}/{tot}':<14} | {f'{diag_abst}/{tot}':<14} | "
            f"{f'{act_corr}/{tot}':<14} | {f'{act_unnec}/{tot}':<12} | {f'{errors}/{tot}':<8}"
        )

    # 2. Per-Family Breakdown
    lines.append("\n[2] PER-FAMILY BREAKDOWN")
    lines.append("-" * 80)
    lines.append(
        f"{'Variant':<8} | {'Family':<26} | {'Diag Correct':<14} | {'Action Correct':<14}"
    )
    lines.append("-" * 80)

    for v in variants:
        for fam in families:
            vf_runs = [
                r for r in results
                if r["variant"] == v and scenario_family_map.get(r["scenario_id"]) == fam
            ]
            tot = len(vf_runs)
            if tot == 0:
                continue
            diag_corr = sum(1 for r in vf_runs if r["grades"]["diagnosis"]["is_correct"])
            act_corr = sum(1 for r in vf_runs if r["grades"]["action"]["correct"])
            lines.append(
                f"{v.upper():<8} | {fam:<26} | "
                f"{f'{diag_corr}/{tot}':<14} | {f'{act_corr}/{tot}':<14}"
            )

    # 3. Failures & Discrepancies Listing
    lines.append("\n[3] FAILURES & DISCREPANCIES LISTING")
    lines.append("-" * 80)
    failure_count = 0
    for r in results:
        v = r["variant"].upper()
        s_id = r["scenario_id"]
        trial = r["trial"]
        d_grade = r["grades"]["diagnosis"]["grade"]
        a_grade = (
            "correct" if r["grades"]["action"]["correct"]
            else "unnecessary" if r["grades"]["action"]["unnecessary"]
            else "wrong_target" if r["grades"]["action"]["wrong_target"]
            else "missing"
        )
        is_diag_fail = d_grade in ("wrong", "abstained")
        is_act_fail = not r["grades"]["action"]["correct"]
        err = r.get("errors")

        if is_diag_fail or is_act_fail or err:
            failure_count += 1
            reasons = []
            if err:
                reasons.append(f"error: {err}")
            if is_diag_fail:
                reasons.append(f"diagnosis {d_grade}")
            if is_act_fail:
                reasons.append(f"action {a_grade}")
            reasons_str = "; ".join(reasons)
            lines.append(f" - [{v}] Scenario {s_id} (trial {trial}): {reasons_str}")

    if failure_count == 0:
        lines.append(" No failures or discrepancies recorded.")

    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> None:
    """CLI entrypoint for evals.baseline_runner."""
    parser = argparse.ArgumentParser(
        description="Run evaluation harness over baseline ladder variants (B0, B1, B2)."
    )
    parser.add_argument(
        "--variants",
        type=str,
        default="b0,b1,b2",
        help="Comma-separated variants to evaluate (e.g. 'b0,b1,b2').",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default="all",
        help="Comma-separated scenarios to evaluate (e.g. 'all' or 'S1,S3').",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials per scenario and variant (default: 1).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory path for per-run JSON and summary.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run offline using snapshot backend and FakeLLMClient.",
    )

    args = parser.parse_args()
    variants = [v.strip().lower() for v in args.variants.split(",") if v.strip()]
    scenarios = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    out_dir = Path(args.out) if args.out else None

    runner = BaselineRunner(
        variants=variants,
        scenario_ids=scenarios,
        trials=args.trials,
        out_dir=out_dir,
        offline=args.offline,
    )

    results = asyncio.run(runner.run())
    scenarios_loaded = runner.load_scenarios()
    report = aggregate_results(results, scenarios_loaded)
    print(report)


if __name__ == "__main__":
    main()
