"""Runs B0 and B1 once per scenario (S1 - S5) and prints raw output."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baseline.alerts.evaluator import evaluate, load_rules
from baseline.b0_alert_only import run_b0
from baseline.b1_runbook import run_b1
from tests.test_baseline_alerts import (
    build_snapshot_file,
    generate_s1_snapshot,
    generate_s2_snapshot,
    generate_s3_snapshot,
    generate_s4_snapshot,
    generate_s5_snapshot,
)
from tools.read.backends.snapshot import SnapshotBackend


def setup_scenario_fixture(
    base_dir: Path,
    scenario_id: str,
    base_time: datetime,
) -> SnapshotBackend:
    deploys_dir = base_dir / "deploys"
    deploys_dir.mkdir(parents=True, exist_ok=True)
    deploys_file = deploys_dir / "deploys.json"

    if scenario_id == "S1":
        generate_s1_snapshot(base_dir, base_time)
        deploys = [
            {
                "deploy_id": "deploy_orders_v130",
                "service": "orders",
                "version": "v1.3.0",
                "deployed_at": (base_time + timedelta(seconds=15)).isoformat(),
                "git_commit": "def5678",
                "status": "completed",
            }
        ]
        deploys_file.write_text(json.dumps(deploys), encoding="utf-8")

    elif scenario_id == "S2":
        generate_s2_snapshot(base_dir, base_time)
        deploys = [
            {
                "deploy_id": "deploy_inventory_v120",
                "service": "inventory",
                "version": "v1.2.0",
                "deployed_at": (base_time + timedelta(seconds=15)).isoformat(),
                "git_commit": "abc1234",
                "status": "completed",
            }
        ]
        deploys_file.write_text(json.dumps(deploys), encoding="utf-8")

    elif scenario_id == "S3":
        generate_s3_snapshot(base_dir, base_time)
        # Add dependency latency metric for inventory (redis degradation)
        dep_pts = []
        for step in range(41):
            s = step * 15
            t = (base_time + timedelta(seconds=s)).isoformat()
            val = 0.005 if s < 20 else 0.32
            dep_pts.append({"timestamp": t, "value": val})
        build_snapshot_file(base_dir, "inventory", "dependency_latency_p95", dep_pts)
        deploys_file.write_text(json.dumps([]), encoding="utf-8")

    elif scenario_id == "S4":
        generate_s4_snapshot(base_dir, base_time)
        # Add dependency latency metric for inventory
        dep_pts = []
        for step in range(41):
            s = step * 15
            t = (base_time + timedelta(seconds=s)).isoformat()
            val = 0.005 if s < 30 else 0.32
            dep_pts.append({"timestamp": t, "value": val})
        build_snapshot_file(base_dir, "inventory", "dependency_latency_p95", dep_pts)
        deploys = [
            {
                "deploy_id": "decoy_deploy_orders_v110",
                "service": "orders",
                "version": "v1.1.0",
                "deployed_at": (base_time + timedelta(seconds=5)).isoformat(),
                "git_commit": "decoy001",
                "status": "completed",
            }
        ]
        deploys_file.write_text(json.dumps(deploys), encoding="utf-8")

    elif scenario_id == "S5":
        generate_s5_snapshot(base_dir, base_time)
        deploys_file.write_text(json.dumps([]), encoding="utf-8")

    else:
        raise ValueError(f"Unknown scenario {scenario_id}")

    backend = SnapshotBackend(base_dir)
    return backend


async def main() -> None:
    base_time = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    rules = load_rules()

    results: dict[str, Any] = {}

    for s_id in ["S1", "S2", "S3", "S4", "S5"]:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_path = Path(tmp_str)
            backend = setup_scenario_fixture(tmp_path, s_id, base_time)
            window = (base_time, base_time + timedelta(minutes=10))

            alerts = await evaluate(rules, backend, window)
            # Pick the primary incident alert
            if s_id in ("S1", "S5"):
                primary_alert = [a for a in alerts if a.rule_id == "orders_db_pool_saturation"][0]
            else:
                primary_alert = [a for a in alerts if a.rule_id == "inventory_latency_p95_high"][0]

            # Run B0
            res_b0 = run_b0(primary_alert, scenario_id=s_id)

            # Run B1
            res_b1 = await run_b1(primary_alert, backend=backend, scenario_id=s_id)

            results[s_id] = {
                "scenario_id": s_id,
                "alert": primary_alert.model_dump(mode="json"),
                "B0": res_b0.model_dump(mode="json"),
                "B1": res_b1.model_dump(mode="json"),
            }

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
