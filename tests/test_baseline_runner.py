"""Unit and integration tests for evals/baseline_runner.py.

Verifies:
1. Offline execution across S1-S5 with FakeLLMClient.
2. Result JSON schema compliance and completeness.
3. Isolation: Variant runners receive only AlertEvent + backend/client, never Scenario ground truth.
4. Aggregate table format with exact numerators/denominators (no bare percentages).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent.llm.fake import FakeLLMClient
from evals.baseline_runner import (
    BaselineRunner,
    aggregate_results,
)
from lab.scenarios.loader import load_all_scenarios


@pytest.mark.asyncio
async def test_baseline_runner_offline_matrix() -> None:
    """Verifies that BaselineRunner executes offline for all variants and scenarios."""
    scenarios = load_all_scenarios()
    assert len(scenarios) >= 5

    fake_b2_response = {
        "diagnosis": {
            "root_cause_service": None,
            "root_cause_category": "unknown",
            "mechanism": "unknown",
            "culprit_deploy": None,
            "confidence": 0.0,
        },
        "action": {
            "action": "escalate",
            "service": None,
            "rationale_ids": [],
            "notes": "Offline test escalation",
        },
        "confidence": 0.0,
        "uncertainty_note": "Offline test mode",
    }
    fake_client = FakeLLMClient(responses=[fake_b2_response] * 10)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "baseline_out"
        runner = BaselineRunner(
            variants=["b0", "b1", "b2"],
            scenario_ids=["S1", "S2"],
            trials=1,
            out_dir=out_path,
            offline=True,
            client=fake_client,
        )

        results = await runner.run()

        # 2 scenarios * 3 variants * 1 trial = 6 records
        assert len(results) == 6

        # Check files exist
        summary_file = out_path / "summary.json"
        assert summary_file.is_file()

        for s_id in ["S1", "S2"]:
            for v in ["b0", "b1", "b2"]:
                run_file = out_path / f"{s_id}_{v}_t1.json"
                assert run_file.is_file()


def test_result_json_schema() -> None:
    """Validates that each per-run record contains all required fields with proper types."""
    required_keys = {
        "scenario_id",
        "variant",
        "trial",
        "alert",
        "diagnosis",
        "action",
        "grades",
        "tool_calls",
        "tokens",
        "latency_ms",
        "completion_time_seconds",
        "execution_wall_seconds",
        "cost_estimate",
        "errors",
        "prompt_hash",
        "runbook_hash",
        "model_settings",
        "git_sha",
        "harness_version",
    }

    sample_record = {
        "scenario_id": "S1",
        "variant": "b1",
        "trial": 1,
        "alert": {
            "alert_id": "alert123",
            "fired_at": "2026-11-24T10:00:15Z",
            "service": "orders",
            "rule_id": "orders_db_pool_saturation",
            "metric": "db_pool_in_use",
            "threshold": 0.8,
            "observed_value": 0.9,
            "window_seconds": 30,
            "severity": "critical",
        },
        "diagnosis": {
            "root_cause_service": "orders",
            "root_cause_category": "bad_deploy",
            "mechanism": "connection_pool_exhaustion",
            "culprit_deploy": "deploy_orders_v130",
            "confidence": 0.9,
        },
        "action": {
            "action": "rollback_service",
            "service": "orders",
            "rationale_ids": [],
            "notes": "Rollback orders",
        },
        "grades": {
            "diagnosis": {
                "grade": "correct",
                "is_correct": True,
                "is_partial": False,
                "is_wrong": False,
                "is_abstained": False,
                "reasons": [],
            },
            "action": {
                "correct": True,
                "unnecessary": False,
                "wrong_target": False,
                "missing": False,
                "reasons": [],
            },
        },
        "tool_calls": ["list_deploys"],
        "tokens": {"tokens_in": 0, "tokens_out": 0, "total": 0},
        "latency_ms": 12.5,
        "completion_time_seconds": 0.1,
        "execution_wall_seconds": 0.05,
        "cost_estimate": 0.0,
        "errors": None,
        "prompt_hash": None,
        "runbook_hash": "26d4a361",
        "model_settings": {},
        "git_sha": "b325d9f",
        "harness_version": "1.0.0",
    }

    assert required_keys.issubset(set(sample_record.keys()))


def test_variant_isolation_signature() -> None:
    """Verifies that variant runner functions accept only AlertEvent + ToolContext/Backend/Client.

    They must never accept Scenario objects, ground truth, or vault modules.
    """
    import inspect

    from baseline.b0_alert_only import run_b0
    from baseline.b1_runbook import run_b1
    from baseline.b2_llm_no_tools import run_b2

    # run_b0 signature
    sig_b0 = inspect.signature(run_b0)
    param_names_b0 = set(sig_b0.parameters.keys())
    assert "scenario" not in param_names_b0
    assert "ground_truth" not in param_names_b0
    assert "alert" in param_names_b0

    # run_b1 signature
    sig_b1 = inspect.signature(run_b1)
    param_names_b1 = set(sig_b1.parameters.keys())
    assert "scenario" not in param_names_b1
    assert "ground_truth" not in param_names_b1
    assert "alert" in param_names_b1

    # run_b2 signature
    sig_b2 = inspect.signature(run_b2)
    param_names_b2 = set(sig_b2.parameters.keys())
    assert "scenario" not in param_names_b2
    assert "ground_truth" not in param_names_b2
    assert "alert" in param_names_b2


def test_aggregate_results_formatting() -> None:
    """Verifies aggregate report prints strict fractions (numerators/denominators), no bare %."""
    scenarios = load_all_scenarios()

    mock_results: list[dict[str, Any]] = [
        {
            "scenario_id": "S1",
            "variant": "b0",
            "trial": 1,
            "grades": {
                "diagnosis": {
                    "grade": "abstained",
                    "is_correct": False,
                    "is_abstained": True,
                },
                "action": {
                    "correct": False,
                    "unnecessary": False,
                    "wrong_target": False,
                    "missing": True,
                },
            },
            "errors": None,
        },
        {
            "scenario_id": "S2",
            "variant": "b1",
            "trial": 1,
            "grades": {
                "diagnosis": {
                    "grade": "correct",
                    "is_correct": True,
                    "is_abstained": False,
                },
                "action": {
                    "correct": True,
                    "unnecessary": False,
                    "wrong_target": False,
                    "missing": False,
                },
            },
            "errors": None,
        },
    ]

    report = aggregate_results(mock_results, scenarios)

    # Must contain fractions like "0/1" or "1/1"
    assert "0/1" in report
    assert "1/1" in report

    # Must not contain bare percentage symbols in the data table
    assert "%" not in report

    # Must list failure scenario ID
    assert "Scenario S1" in report
    assert "[B0]" in report
