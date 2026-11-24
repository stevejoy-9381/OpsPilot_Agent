"""CLI entrypoint for baseline threshold alerting."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from baseline.alerts.evaluator import evaluate, load_rules
from baseline.alerts.models import AlertEvent
from tools.read.backends.snapshot import SnapshotBackend


def find_snapshot_dir_for_run(
    run_id: str,
    search_roots: Sequence[Path | str] | None = None,
) -> Path | None:
    """Discovers the telemetry/snapshot directory for a given run ID."""
    roots = (
        [Path(r) for r in search_roots]
        if search_roots
        else [
            Path("fixtures/evidence") / run_id,
            Path("fixtures/runs") / run_id,
            Path("telemetry") / run_id,
            Path("runs") / run_id,
            Path(run_id),
        ]
    )

    for root in roots:
        if (root / "metrics").exists():
            return root
    return None


async def run_evaluate_cli(
    run_id: str,
    rules_path: str | None = None,
    data_dir: str | None = None,
    format_type: str = "table",
    window_minutes: int = 15,
) -> list[AlertEvent]:
    """CLI execution function for evaluate command."""
    rules = load_rules(rules_path)

    # Locate snapshot data directory
    if data_dir:
        snap_path = Path(data_dir)
    else:
        found = find_snapshot_dir_for_run(run_id)
        if not found:
            raise FileNotFoundError(
                f"Could not find snapshot directory for run '{run_id}'. "
                "Please specify --data-dir <path>."
            )
        snap_path = found

    backend = SnapshotBackend(snap_path)

    # Define query window around current or recorded timestamps
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=window_minutes)
    end_time = now

    # Check for timestamps in the snapshot files if present
    metrics_dir = snap_path / "metrics"
    if metrics_dir.exists():
        for mf in metrics_dir.glob("*.json"):
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                raw_series = data if isinstance(data, list) else data.get("series", [data])
                for s in raw_series:
                    pts = s.get("points", [])
                    if pts:
                        t_first = pts[0].get("timestamp")
                        t_last = pts[-1].get("timestamp")
                        dt_start = (
                            datetime.fromisoformat(t_first)
                            if isinstance(t_first, str)
                            else datetime.fromtimestamp(t_first, tz=timezone.utc)
                        )
                        dt_end = (
                            datetime.fromisoformat(t_last)
                            if isinstance(t_last, str)
                            else datetime.fromtimestamp(t_last, tz=timezone.utc)
                        )
                        start_time = min(start_time, dt_start)
                        end_time = max(end_time, dt_end)
            except Exception:
                pass

    alerts = await evaluate(
        rules=rules,
        metrics_backend=backend,
        window=(start_time, end_time),
    )

    if format_type == "json":
        output = [a.model_dump(mode="json") for a in alerts]
        print(json.dumps(output, indent=2))
    else:
        # Table output
        print(f"\nEvaluated {len(rules)} rules against run '{run_id}' (Snapshot: {snap_path})")
        print(f"Time Window: {start_time.isoformat()} to {end_time.isoformat()}")
        print("-" * 105)
        print(
            f"{'ALERT ID':<16} {'FIRED AT':<24} {'SERVICE':<12} {'RULE ID':<30} "
            f"{'VALUE':<8} {'STATUS':<10}"
        )
        print("-" * 105)
        if not alerts:
            print("No threshold alerts fired. System is healthy within defined tolerances.")
        else:
            for a in alerts:
                fired_str = a.fired_at.strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"{a.alert_id:<16} {fired_str:<24} {a.service:<12} {a.rule_id:<30} "
                    f"{a.observed_value:<8.2f} {a.status:<10}"
                )
        print("-" * 105)

    return alerts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baseline.alerts",
        description="Deterministic threshold alert evaluation CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    eval_parser = subparsers.add_parser("evaluate", help="Evaluate alert rules on a run.")
    eval_parser.add_argument("--run", required=True, help="Run identifier to evaluate")
    eval_parser.add_argument("--rules", default=None, help="Path to rules.yaml")
    eval_parser.add_argument("--data-dir", default=None, help="Explicit snapshot directory")
    eval_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (table or json)",
    )
    eval_parser.add_argument(
        "--window-minutes",
        type=int,
        default=15,
        help="Evaluation lookback window in minutes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "evaluate":
        try:
            asyncio.run(
                run_evaluate_cli(
                    run_id=args.run,
                    rules_path=args.rules,
                    data_dir=args.data_dir,
                    format_type=args.format,
                    window_minutes=args.window_minutes,
                )
            )
            return 0
        except Exception as exc:
            print(f"Error evaluating alerts: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
