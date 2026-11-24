"""Comprehensive unit tests for baseline runners B0 (Alert-Only) and B1 (Runbook).

Verifies:
1. B0 produces abstained diagnosis and escalate action.
2. B1 evaluates R1 (latency/error + deploy within 30m -> rollback).
3. B1 evaluates R2 (error/latency + no deploy + elevated dependency -> restart dependency).
4. B1 evaluates R3 (saturation + deploy within 60m -> rollback).
5. B1 evaluates R4 (alert resolved -> no_action).
6. B1 evaluates R5 (no rule matched -> default escalate).
7. Budget cap of 4 calls is strictly enforced.
8. Determinism: identical input produces identical diagnosis, action, and tool calls.
9. Zero LLM dependency: FakeLLMClient and LLM packages are completely absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from baseline.alerts.models import AlertEvent
from baseline.b0_alert_only import run_b0
from baseline.b1_runbook import run_b1
from shared.actions import ActionType
from tools.common.evidence_store import InMemoryEvidenceStore
from tools.read.backends.snapshot import SnapshotBackend


def build_backend_fixture(
    tmp_path: Path,
    deploys: list[dict[str, Any]] | None = None,
    metrics: dict[str, list[dict[str, Any]]] | None = None,
) -> SnapshotBackend:
    """Helper creating a temporary snapshot directory with deploys and metrics."""
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    deploys_dir = tmp_path / "deploys"
    deploys_dir.mkdir(parents=True, exist_ok=True)

    # Write deploys
    deploys_file = deploys_dir / "deploys.json"
    deploys_file.write_text(json.dumps(deploys or []), encoding="utf-8")

    # Write metrics
    if metrics:
        for fname, pts in metrics.items():
            m_path = metrics_dir / f"{fname}.json"
            parts = fname.split("_", 1)
            svc = parts[0]
            mname = parts[1] if len(parts) > 1 else fname
            data = {"metric_name": mname, "service": svc, "points": pts}
            m_path.write_text(json.dumps(data), encoding="utf-8")

    return SnapshotBackend(tmp_path)


# -----------------------------------------------------------------------------
# 1. B0 Alert-Only Tests
# -----------------------------------------------------------------------------

def test_b0_returns_escalate_and_abstained() -> None:
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_test_001",
        fired_at=now,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=8.5,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
    )

    res = run_b0(alert, scenario_id="S1")

    assert res.baseline_name == "B0"
    assert res.scenario_id == "S1"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.diagnosis.mechanism == "unknown"
    assert res.diagnosis.confidence == 0.0
    assert res.action.action == ActionType.ESCALATE
    assert res.action.service is None
    assert len(res.tool_calls) == 0
    assert res.tokens_in == 0
    assert res.tokens_out == 0
    assert res.cost_usd == 0.0


# -----------------------------------------------------------------------------
# 2. B1 Runbook Tests for Rules R1 - R5
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b1_rule_r1_latency_with_recent_deploy(tmp_path: Path) -> None:
    """R1: Latency alert on inventory + deploy to inventory 10m ago -> rollback inventory."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_s2_001",
        fired_at=now,
        service="inventory",
        rule_id="inventory_latency_p95_high",
        metric="latency_p95",
        threshold=0.15,
        observed_value=0.35,
        window_seconds=15,
        severity="WARNING",
        status="firing",
    )

    deploys = [
        {
            "deploy_id": "deploy_inv_v120",
            "service": "inventory",
            "version": "v1.2.0",
            "deployed_at": (now - timedelta(minutes=10)).isoformat(),
            "git_commit": "abc1234",
            "status": "active",
        }
    ]

    backend = build_backend_fixture(tmp_path, deploys=deploys)
    res = await run_b1(alert, backend)

    assert res.rule_matched == "R1"
    assert res.diagnosis.root_cause_category == "bad_deploy"
    assert res.diagnosis.root_cause_service == "inventory"
    assert res.diagnosis.culprit_deploy_id == "deploy_inv_v120"
    assert res.action.action == ActionType.ROLLBACK_SERVICE
    assert res.action.service == "inventory"
    assert "list_deploys" in res.tool_calls
    assert len(res.tool_calls) <= 4


@pytest.mark.asyncio
async def test_b1_rule_r2_downstream_dependency_elevated(tmp_path: Path) -> None:
    """R2: Latency alert on inventory, no deploy, redis dependency elevated -> restart redis."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_s3_001",
        fired_at=now,
        service="inventory",
        rule_id="inventory_latency_p95_high",
        metric="latency_p95",
        threshold=0.15,
        observed_value=0.35,
        window_seconds=15,
        severity="WARNING",
        status="firing",
    )

    metrics = {
        "inventory_latency_p95": [
            {"timestamp": (now - timedelta(seconds=15)).isoformat(), "value": 0.35},
            {"timestamp": now.isoformat(), "value": 0.35},
        ],
        "inventory_dependency_latency_p95": [
            {"timestamp": (now - timedelta(seconds=15)).isoformat(), "value": 0.30},
            {"timestamp": now.isoformat(), "value": 0.32},
        ],
    }

    backend = build_backend_fixture(tmp_path, deploys=[], metrics=metrics)
    res = await run_b1(alert, backend)

    assert res.rule_matched == "R2"
    assert res.diagnosis.root_cause_category == "dependency_failure"
    assert res.diagnosis.root_cause_service == "redis"
    assert res.action.action == ActionType.RESTART_SERVICE
    assert res.action.service == "redis"
    assert len(res.tool_calls) <= 4


@pytest.mark.asyncio
async def test_b1_rule_r3_saturation_with_deploy(tmp_path: Path) -> None:
    """R3: Saturation alert on orders + deploy to orders 45m ago -> rollback orders."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_s1_001",
        fired_at=now,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=10.0,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
    )

    deploys = [
        {
            "deploy_id": "deploy_ord_v130",
            "service": "orders",
            "version": "v1.3.0",
            "deployed_at": (now - timedelta(minutes=45)).isoformat(),
            "git_commit": "def5678",
            "status": "active",
        }
    ]

    backend = build_backend_fixture(tmp_path, deploys=deploys)
    res = await run_b1(alert, backend)

    assert res.rule_matched == "R3"
    assert res.diagnosis.root_cause_category == "bad_deploy"
    assert res.diagnosis.root_cause_service == "orders"
    assert res.diagnosis.mechanism == "connection_pool_exhaustion"
    assert res.action.action == ActionType.ROLLBACK_SERVICE
    assert res.action.service == "orders"
    assert len(res.tool_calls) <= 4


@pytest.mark.asyncio
async def test_b1_rule_r4_resolved_alert_no_action(tmp_path: Path) -> None:
    """R4: Transient alert that is resolved before evaluation -> no_action."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_s5_001",
        fired_at=now,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=8.5,
        window_seconds=15,
        severity="CRITICAL",
        status="resolved",  # Already marked resolved
        resolved_at=now + timedelta(seconds=15),
    )

    backend = build_backend_fixture(tmp_path, deploys=[])
    res = await run_b1(alert, backend)

    assert res.rule_matched == "R4"
    assert res.diagnosis.root_cause_category == "false_alarm"
    assert res.diagnosis.mechanism == "none"
    assert res.action.action == ActionType.NO_ACTION
    assert res.action.service is None


@pytest.mark.asyncio
async def test_b1_rule_r5_default_escalation(tmp_path: Path) -> None:
    """R5: No recent deploy, metric not resolved, no downstream elevated -> escalate."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_generic_001",
        fired_at=now,
        service="gateway",
        rule_id="gateway_error_rate_high",
        metric="error_rate",
        threshold=0.05,
        observed_value=0.20,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
    )

    # Still elevated metric, no deploys, normal downstream
    metrics = {
        "gateway_error_rate": [
            {"timestamp": now.isoformat(), "value": 0.20},
        ],
        "inventory_latency_p95": [
            {"timestamp": now.isoformat(), "value": 0.015},
        ],
    }

    backend = build_backend_fixture(tmp_path, deploys=[], metrics=metrics)
    res = await run_b1(alert, backend)

    assert res.rule_matched == "R5"
    assert res.diagnosis.root_cause_category == "unknown"
    assert res.action.action == ActionType.ESCALATE
    assert res.action.service is None
    assert len(res.tool_calls) <= 4


@pytest.mark.asyncio
async def test_b1_budget_cap_enforced(tmp_path: Path) -> None:
    """Verifies that B1 strictly respects the budget cap of 4 tool calls."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_budget_001",
        fired_at=now,
        service="gateway",
        rule_id="gateway_latency_p95_high",
        metric="latency_p95",
        threshold=0.25,
        observed_value=0.40,
        window_seconds=15,
        severity="WARNING",
        status="firing",
    )

    metrics = {
        "gateway_latency_p95": [{"timestamp": now.isoformat(), "value": 0.40}],
        "inventory_latency_p95": [{"timestamp": now.isoformat(), "value": 0.35}],
        "inventory_dependency_latency_p95": [{"timestamp": now.isoformat(), "value": 0.30}],
    }

    backend = build_backend_fixture(tmp_path, deploys=[], metrics=metrics)
    store = InMemoryEvidenceStore()
    res = await run_b1(alert, backend, store=store, max_calls=4)

    assert len(res.tool_calls) <= 4


def test_no_llm_dependency() -> None:
    """Verifies that B0 and B1 execute with zero LLM dependencies or imports."""
    import baseline.b0_alert_only as b0_mod
    import baseline.b1_runbook as b1_mod

    for mod in [b0_mod, b1_mod]:
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "FakeLLMClient" not in src
        assert "google.genai" not in src
        assert "langgraph" not in src
        assert "llm" not in src.lower() or "no llm" in src.lower() or "zero llm" in src.lower()


@pytest.mark.asyncio
async def test_b1_determinism(tmp_path: Path) -> None:
    """Verifies that B1 produces identical outputs given identical input."""
    now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        alert_id="alt_det_001",
        fired_at=now,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=10.0,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
    )

    deploys = [
        {
            "deploy_id": "deploy_ord_v130",
            "service": "orders",
            "version": "v1.3.0",
            "deployed_at": (now - timedelta(minutes=20)).isoformat(),
            "git_commit": "def5678",
            "status": "active",
        }
    ]

    backend = build_backend_fixture(tmp_path, deploys=deploys)
    res1 = await run_b1(alert, backend)
    res2 = await run_b1(alert, backend)

    assert res1.rule_matched == res2.rule_matched
    assert res1.diagnosis.root_cause_service == res2.diagnosis.root_cause_service
    assert res1.diagnosis.culprit_deploy_id == res2.diagnosis.culprit_deploy_id
    assert res1.action.action == res2.action.action
    assert res1.action.service == res2.action.service
    assert res1.tool_calls == res2.tool_calls
