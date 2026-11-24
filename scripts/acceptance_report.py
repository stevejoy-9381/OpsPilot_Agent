import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from baseline.alerts.evaluator import evaluate, load_rules
from tests.test_baseline_alerts import (
    generate_clean_steady_snapshot,
    generate_s1_snapshot,
    generate_s2_snapshot,
    generate_s3_snapshot,
    generate_s4_snapshot,
    generate_s5_snapshot,
)
from tools.read.backends.snapshot import SnapshotBackend


async def print_acceptance() -> None:
    rules = load_rules()
    base_time = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
    scenarios = [
        ("Clean Run", generate_clean_steady_snapshot, None, "Healthy Baseline (steady 20 RPS)"),
        ("S1", generate_s1_snapshot, 15, "Orders pool leak (deploy v1.3.0 at t=15s)"),
        ("S2", generate_s2_snapshot, 15, "Inventory cache bypass (deploy v1.2.0 at t=15s)"),
        ("S3", generate_s3_snapshot, 20, "Redis dependency fault (injected at t=20s)"),
        ("S4", generate_s4_snapshot, 30, "Redis fault at t=30s (Orders decoy at t=5s)"),
        ("S5", generate_s5_snapshot, 20, "Marketing burst 5x (surge at t=20s, self-resolved)"),
    ]

    print("\n" + "=" * 105)
    print("BASELINE ALERT ACCEPTANCE REPORT: SCENARIO ALERTS RELATIVE TO INJECTION TIME")
    print("=" * 105)

    for scn_id, gen_fn, injection_offset, desc in scenarios:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gen_fn(tmp_path, base_time)
            backend = SnapshotBackend(tmp_path)
            window = (base_time, base_time + timedelta(minutes=10))
            alerts = await evaluate(rules, backend, window)

            print(f"\nScenario: {scn_id} - {desc}")
            if injection_offset is not None:
                inj_time = base_time + timedelta(seconds=injection_offset)
                inj_str = inj_time.strftime("%H:%M:%S")
                print(f"  Injection Timestamp: {inj_str} (offset: +{injection_offset}s)")
            else:
                print("  Injection: None (Healthy Baseline Traffic)")

            if not alerts:
                print("  -> ALERTS FIRED: None (0 alerts fired). System healthy.")
            else:
                for a in alerts:
                    delta_s = (a.fired_at - base_time).total_seconds()
                    rel_inj = (
                        f"+{int(delta_s - injection_offset)}s"
                        if injection_offset is not None
                        else "N/A"
                    )
                    res_str = ""
                    if a.status == "resolved" and a.resolved_at:
                        res_delta = (a.resolved_at - base_time).total_seconds()
                        inj_delta = int(res_delta - injection_offset)
                        res_str = f" -> RESOLVED at +{int(res_delta)}s (+{inj_delta}s rel to inj)"
                    fired_str = a.fired_at.strftime("%H:%M:%S")
                    print(
                        f"  -> Alert: {a.rule_id:<28} | Target: [{a.service:<9}] | "
                        f"Fired: {fired_str} (+{int(delta_s)}s, {rel_inj:>5} rel) | "
                        f"Obs: {a.observed_value:<6.2f} | Status: {a.status}{res_str}"
                    )

    print("\n" + "=" * 105 + "\n")


if __name__ == "__main__":
    asyncio.run(print_acceptance())
