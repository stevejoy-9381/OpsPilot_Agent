"""Unit tests for OpsPilot Scenario schema, validation rules, and catalog fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from lab.scenarios.loader import load_all_scenarios, load_scenario
from lab.scenarios.models import Scenario


def test_all_five_scenarios_validate() -> None:
    """Verify all five scenario fixtures load, validate, and have exactly one per family."""
    scenarios = load_all_scenarios("fixtures/scenarios")
    assert len(scenarios) == 5, f"Expected exactly 5 scenarios, found {len(scenarios)}"

    scenario_ids = [s.scenario_id for s in scenarios]
    assert scenario_ids == ["S1", "S2", "S3", "S4", "S5"]

    families = {s.family for s in scenarios}
    expected_families = {
        "pool_leak",
        "latency_regression",
        "dependency_timeout",
        "dependency_timeout_with_decoy",
        "false_alarm",
    }
    assert families == expected_families


def test_reject_unknown_field() -> None:
    """Extra fields must be rejected per extra='forbid' constraint."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["unexpected_rogue_field"] = "malicious_data"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    err_str = str(exc_info.value)
    assert "extra_forbidden" in err_str or "unexpected_rogue_field" in err_str


def test_reject_unordered_timeline_offsets() -> None:
    """Timeline events with decreasing offsets must be rejected."""
    sc4 = load_scenario("fixtures/scenarios/S4_dependency_timeout_with_decoy.yml")
    raw_dict: dict[str, Any] = sc4.model_dump()
    # Invert timeline event offsets: event 0 has offset 50, event 1 has offset 10
    raw_dict["timeline"][0]["offset_s"] = 50.0
    raw_dict["timeline"][1]["offset_s"] = 10.0

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "must be ordered by offset_s" in str(exc_info.value)


def test_reject_negative_timeline_offset() -> None:
    """Timeline events with negative offsets must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["timeline"][0]["offset_s"] = -5.0

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "greater than or equal to 0" in str(exc_info.value) or "negative" in str(exc_info.value)


def test_reject_unknown_service_in_baseline() -> None:
    """Baseline versions referencing an unknown service must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["baseline_versions"]["non_existent_microservice"] = "v1.0.0"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "Unknown service 'non_existent_microservice'" in str(exc_info.value)


def test_reject_invalid_version_in_baseline() -> None:
    """Baseline versions referencing an invalid version profile must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["baseline_versions"]["orders"] = "v9.9.9"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "not in baseline_versions" in str(exc_info.value)


def test_reject_invalid_deploy_version() -> None:
    """Deploy events referencing an invalid version profile must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["timeline"][0]["version"] = "v2.0.0-beta"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "references invalid version 'v2.0.0-beta'" in str(exc_info.value)


def test_reject_unmatched_culprit_deploy_reference() -> None:
    """ground_truth.culprit_deploy must match a valid deploy event_id in timeline."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["ground_truth"]["culprit_deploy"] = "deploy_event_does_not_exist"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "does not match any deploy event_id in the timeline" in str(exc_info.value)


def test_reject_invalid_family() -> None:
    """Unknown scenario family must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["family"] = "unsupported_family_type"

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "Unknown scenario family 'unsupported_family_type'" in str(exc_info.value)


def test_reject_single_sentence_narrative() -> None:
    """Narratives with fewer than 2 sentences must be rejected."""
    sc1 = load_scenario("fixtures/scenarios/S1_pool_leak.yml")
    raw_dict: dict[str, Any] = sc1.model_dump()
    raw_dict["narrative"] = "Just one short sentence without a second one."

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(raw_dict)
    assert "must be exactly 2-3 sentences" in str(exc_info.value)
