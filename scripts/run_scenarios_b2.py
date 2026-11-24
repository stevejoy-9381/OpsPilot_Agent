"""Runs B2 live across all scenarios (S1 - S5) if GEMINI_API_KEY is available."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baseline.alerts.evaluator import evaluate, load_rules
from baseline.b2_llm_no_tools import run_b2
from scripts.run_scenarios_b0_b1 import setup_scenario_fixture


async def _run_all() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set. Live execution halted.", file=sys.stderr)
        sys.exit(1)

    base_time = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    rules = load_rules()
    results: dict[str, Any] = {}

    for s_id in ["S1", "S2", "S3", "S4", "S5"]:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_path = Path(tmp_str)
            backend = setup_scenario_fixture(tmp_path, s_id, base_time)
            window = (base_time, base_time + timedelta(minutes=10))

            alerts_list = await evaluate(rules, backend, window)

            if s_id in ("S1", "S5"):
                matches = [a for a in alerts_list if a.rule_id == "orders_db_pool_saturation"]
            else:
                matches = [a for a in alerts_list if a.rule_id == "inventory_latency_p95_high"]
            primary_alert = matches[0]

            res_b2 = run_b2(primary_alert, scenario_id=s_id)
            results[s_id] = {
                "scenario_id": s_id,
                "alert": primary_alert.model_dump(mode="json"),
                "B2": res_b2.model_dump(mode="json"),
            }

    print(json.dumps(results, indent=2))


def main() -> None:
    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
