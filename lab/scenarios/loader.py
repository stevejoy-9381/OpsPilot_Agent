"""Scenario file loader and validator for OpsPilot."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from lab.scenarios.models import Scenario


def load_scenario(path: str | Path) -> Scenario:
    """Loads and validates a single scenario YAML file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Scenario file not found: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Scenario file {p} did not contain a valid mapping/dictionary.")

    return Scenario.model_validate(data)


def load_all_scenarios(dir_path: str | Path = "fixtures/scenarios") -> list[Scenario]:
    """Loads and validates all scenario YAML files within a directory."""
    d = Path(dir_path)
    if not d.is_dir():
        raise FileNotFoundError(f"Scenario directory not found: {d}")

    scenarios: list[Scenario] = []
    # Collect all .yml and .yaml files
    scenario_files = sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml")))

    for f in scenario_files:
        scenarios.append(load_scenario(f))

    # Sort scenarios deterministically by scenario_id
    scenarios.sort(key=lambda s: s.scenario_id)
    return scenarios
