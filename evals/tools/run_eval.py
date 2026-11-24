"""Run evaluation harness over scenarios S1-S5 and execute tool ablation."""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.tools.scripted_investigator import INVESTIGATORS, ToolMeasurement
from tests.test_deploys_tool import _build_deploys_snapshot
from tests.test_logs_tool import _build_log_snapshot
from tests.test_metrics_tool import _build_scenario_snapshot
from tests.test_traces_tool import _build_traces_snapshot
from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ToolContext
from tools.read.backends.snapshot import SnapshotBackend
from tools.registry import ToolRegistry


def build_unified_snapshot(base_dir: Path) -> Path:
    _build_scenario_snapshot(base_dir)
    _build_log_snapshot(base_dir)
    _build_traces_snapshot(base_dir)
    _build_deploys_snapshot(base_dir)
    return base_dir


async def run_eval() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        snap_path = build_unified_snapshot(Path(tmp_dir))
        backend = SnapshotBackend(snap_path)

        all_measurements: list[ToolMeasurement] = []
        scenario_results = {}

        # 1. Base Evaluation
        for scenario_id, inv_func in INVESTIGATORS.items():
            store = InMemoryEvidenceStore()
            ctx = ToolContext(
                run_id=f"run_eval_{scenario_id}",
                tenant_id="tenant_eval",
                incident_id=f"inc_{scenario_id}",
                window_start=datetime(2026, 3, 30, 9, 45, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
                allowed_services={"orders", "inventory", "payment"},
                remaining_calls=8,
            )
            reg = ToolRegistry()

            diag, ev_ids, measurements = await inv_func(reg, ctx, backend, store, disabled_tool=None)
            all_measurements.extend(measurements)
            calls_used = 8 - ctx.remaining_calls

            scenario_results[scenario_id] = {
                "diagnosed": bool(diag),
                "calls_used": calls_used,
                "service": diag.get("service"),
                "category": diag.get("category"),
                "evidence_count": len(ev_ids),
                "within_budget": calls_used <= 8,
            }

        # 2. Tool Metrics Aggregation
        tool_stats = {}
        tool_names = ["get_metrics", "search_logs", "get_trace", "list_deploys", "get_deploy_diff"]
        for tname in tool_names:
            t_meas = [m for m in all_measurements if m.tool == tname]
            if t_meas:
                chars_list = [m.chars for m in t_meas]
                tokens_list = [m.tokens for m in t_meas]
                dur_list = [m.duration_ms for m in t_meas]
                tool_stats[tname] = {
                    "call_count": len(t_meas),
                    "chars_p50": int(statistics.median(chars_list)),
                    "chars_p95": int(max(chars_list)),
                    "tokens_p50": int(statistics.median(tokens_list)),
                    "tokens_p95": int(max(tokens_list)),
                    "avg_duration_ms": round(sum(dur_list) / len(dur_list), 2),
                    "over_budget_count": sum(1 for tok in tokens_list if tok > 1500),
                }

        # 3. Ablation Study
        ablation_matrix = {}
        for disabled in tool_names:
            ablation_matrix[disabled] = {}
            for scenario_id, inv_func in INVESTIGATORS.items():
                store = InMemoryEvidenceStore()
                ctx = ToolContext(
                    run_id=f"run_abl_{scenario_id}_{disabled}",
                    tenant_id="tenant_eval",
                    incident_id=f"inc_{scenario_id}",
                    window_start=datetime(2026, 3, 30, 9, 45, 0, tzinfo=timezone.utc),
                    window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
                    allowed_services={"orders", "inventory", "payment"},
                    remaining_calls=8,
                )
                reg = ToolRegistry()
                diag, _, _ = await inv_func(reg, ctx, backend, store, disabled_tool=disabled)
                ablation_matrix[disabled][scenario_id] = "DIAGNOSED" if diag else "UNDIAGNOSABLE"

        return {
            "scenarios": scenario_results,
            "tool_stats": tool_stats,
            "ablation": ablation_matrix,
        }


if __name__ == "__main__":
    report_data = asyncio.run(run_eval())
    print(json.dumps(report_data, indent=2))
