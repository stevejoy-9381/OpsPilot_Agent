"""Command-line interface for OpsPilot scenarios: validate and list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from lab.scenarios.loader import load_all_scenarios, load_scenario
from lab.scenarios.models import ExpectedAction


def format_action(action: ExpectedAction) -> str:
    """Formats expected action for tabular display."""
    act = action.action
    target = action.service or action.target
    if target:
        return f"{act} ({target})"
    return act


def cmd_validate(args: argparse.Namespace) -> int:
    """Validates scenario YAML files against the schema and semantic rules."""
    path = Path(args.path)
    try:
        if path.is_file():
            scenarios = [load_scenario(path)]
            print(f"Validated scenario '{scenarios[0].scenario_id}' from {path}: OK")
        elif path.is_dir():
            scenarios = load_all_scenarios(path)
            for sc in scenarios:
                print(f"  [OK] {sc.scenario_id:<4} {sc.family:<30} (variant: {sc.variant})")
            print(f"\nAll {len(scenarios)} scenarios validated successfully.")
        else:
            print(f"Error: Path '{path}' does not exist.", file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f"\nValidation Error: {e}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    """Lists scenarios and displays each narrative."""
    path = Path(args.path)
    try:
        scenarios = load_all_scenarios(path)
    except Exception as e:
        print(f"Error loading scenarios from '{path}': {e}", file=sys.stderr)
        return 1

    print("=" * 86)
    print(f"OpsPilot Scenario Catalog ({len(scenarios)} scenarios loaded from {path})")
    print("=" * 86)
    header = f"{'ID':<4} {'Family':<30} {'Variant':<26} {'Seed':<5} {'Expected Action'}"
    print(header)
    print("-" * 86)

    for sc in scenarios:
        act_str = format_action(sc.ground_truth.expected_action)
        print(f"{sc.scenario_id:<4} {sc.family:<30} {sc.variant:<26} {sc.seed:<5} {act_str}")

    print("\n" + "=" * 86)
    print("Scenario Narratives")
    print("=" * 86)

    for sc in scenarios:
        print(f"\n[{sc.scenario_id}] {sc.family} ({sc.variant}):")
        # Cleanly indent narrative text
        clean_narrative = " ".join(sc.narrative.split())
        print(f"  {clean_narrative}")

    print("\n" + "=" * 86)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Constructs the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m lab.scenarios",
        description="OpsPilot Scenario CLI: validate and inspect scenario definitions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Validate subcommand
    val_parser = subparsers.add_parser("validate", help="Validate scenario YAML files.")
    val_parser.add_argument(
        "--path",
        default="fixtures/scenarios",
        help="Path to scenario YAML file or directory (default: fixtures/scenarios)",
    )

    # List subcommand
    list_parser = subparsers.add_parser("list", help="List scenarios and narratives.")
    list_parser.add_argument(
        "--path",
        default="fixtures/scenarios",
        help="Directory path containing scenarios (default: fixtures/scenarios)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "list":
        return cmd_list(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
