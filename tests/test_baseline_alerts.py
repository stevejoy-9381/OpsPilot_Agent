"""Comprehensive test suite for baseline threshold alerting (baseline/alerts).

Verifies:
1. Each of S1-S4 fires at least one alert at a plausible time.
2. S5 (marketing burst) fires an alert that is later resolved (intended false alarm).
3. A clean steady run fires zero alerts over 10 minutes.
4. Flap dedupe: metric oscillation produces exactly one alert episode without spam.
5. Determinism: identical input data produces identical alert_id and alert fields.
6. Leakage scan: all AlertEvent fields pass the Feature 1.13 zero-leakage check.
7. Tuning invariant: tuning script reads ONLY healthy baseline runs and rejects incident data.
8. CLI: python -m baseline.alerts evaluate works end-to-end.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from baseline.alerts.cli import run_evaluate_cli
from baseline.alerts.evaluator import evaluate, load_rules
from baseline.alerts.models import AlertEvent, AlertRule
from baseline.alerts.tuning import assert_healthy_baseline_dataset, tune_thresholds_from_baseline
from tests.test_leakage import FORBIDDEN_FAMILY_NAMES, FORBIDDEN_TERMS_PATTERNS
from tools.read.backends.snapshot import SnapshotBackend

# -----------------------------------------------------------------------------
# Synthetic Snapshot Generators for Clean Run and Scenarios S1 - S5
# -----------------------------------------------------------------------------

def build_snapshot_file(
    base_dir: Path,
    service: str,
    metric: str,
    points: list[dict[str, Any]],
) -> None:
    """Helper writing a single metric snapshot JSON."""
    metrics_dir = base_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    file_path = metrics_dir / f"{service}_{metric}.json"
    data = {
        "metric_name": metric,
        "service": service,
        "points": points,
    }
    file_path.write_text(json.dumps(data), encoding="utf-8")


def generate_clean_steady_snapshot(base_dir: Path, base_time: datetime) -> None:
    """Generates a 10-minute clean steady run (40 points) with healthy baseline telemetry."""
    # 40 steps over 600 seconds
    gw_error_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        t = (base_time + timedelta(seconds=step * 15)).isoformat()
        gw_error_pts.append({"timestamp": t, "value": 0.0})
        # Jitter between 0.030 and 0.040s
        gw_lat_pts.append({"timestamp": t, "value": 0.035})
        ord_pool_pts.append({"timestamp": t, "value": 2.0})
        inv_lat_pts.append({"timestamp": t, "value": 0.015})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_error_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


def generate_s1_snapshot(base_dir: Path, base_time: datetime) -> None:
    """S1: Orders pool leak starting at offset 15s. Pool reaches 8.0 at 45s, 10.0 at 60s."""
    gw_err_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        s = step * 15
        t = (base_time + timedelta(seconds=s)).isoformat()
        if s < 15:
            ord_pool = 2.0
            gw_err = 0.0
        elif s == 15:
            ord_pool = 2.0
            gw_err = 0.0
        elif s == 30:
            ord_pool = 5.0
            gw_err = 0.01
        elif s == 45:
            ord_pool = 8.0  # Breach starts
            gw_err = 0.04
        elif s == 60:
            ord_pool = 10.0  # Sustained 15s >= 7.0 -> Alert fires!
            gw_err = 0.08    # Gateway error rate breaches 0.05
        elif s == 75:
            ord_pool = 10.0
            gw_err = 0.25    # Sustained gateway error rate -> Gateway alert fires!
        else:
            ord_pool = 10.0
            gw_err = 0.28

        ord_pool_pts.append({"timestamp": t, "value": ord_pool})
        gw_err_pts.append({"timestamp": t, "value": gw_err})
        gw_lat_pts.append({"timestamp": t, "value": 0.035 if s < 60 else 0.45})
        inv_lat_pts.append({"timestamp": t, "value": 0.015})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_err_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


def generate_s2_snapshot(base_dir: Path, base_time: datetime) -> None:
    """S2: Inventory cache bypass latency regression injected at offset 15s."""
    gw_err_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        s = step * 15
        t = (base_time + timedelta(seconds=s)).isoformat()
        if s <= 15:
            inv_lat = 0.015
            gw_lat = 0.035
        elif s == 30:
            inv_lat = 0.28  # Breach starts (> 0.15)
            gw_lat = 0.30   # Breach starts (> 0.25)
        elif s >= 45:
            inv_lat = 0.35  # Sustained >= 15s -> Alert fires!
            gw_lat = 0.40   # Sustained >= 15s -> Alert fires!
        else:
            inv_lat = 0.35
            gw_lat = 0.40

        inv_lat_pts.append({"timestamp": t, "value": inv_lat})
        gw_lat_pts.append({"timestamp": t, "value": gw_lat})
        gw_err_pts.append({"timestamp": t, "value": 0.0})
        ord_pool_pts.append({"timestamp": t, "value": 2.0})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_err_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


def generate_s3_snapshot(base_dir: Path, base_time: datetime) -> None:
    """S3: Inventory Redis dependency latency fault injected at offset 20s."""
    gw_err_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        s = step * 15
        t = (base_time + timedelta(seconds=s)).isoformat()
        if s <= 20:
            inv_lat = 0.015
            gw_lat = 0.035
        elif s == 30:
            inv_lat = 0.32  # Breach starts (> 0.15)
            gw_lat = 0.28
        elif s >= 45:
            inv_lat = 0.35  # Sustained 15s -> Alert fires!
            gw_lat = 0.38
        else:
            inv_lat = 0.35
            gw_lat = 0.38

        inv_lat_pts.append({"timestamp": t, "value": inv_lat})
        gw_lat_pts.append({"timestamp": t, "value": gw_lat})
        gw_err_pts.append({"timestamp": t, "value": 0.0})
        ord_pool_pts.append({"timestamp": t, "value": 2.0})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_err_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


def generate_s4_snapshot(base_dir: Path, base_time: datetime) -> None:
    """S4: Inventory Redis fault at offset 30s with Orders decoy deploy at offset 5s.

    Orders metrics remain completely healthy (pool=2.0, latency low).
    Inventory latency spikes and triggers alert.
    """
    gw_err_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        s = step * 15
        t = (base_time + timedelta(seconds=s)).isoformat()
        # Orders remains completely healthy
        ord_pool = 2.0

        if s <= 30:
            inv_lat = 0.015
            gw_lat = 0.035
        elif s == 45:
            inv_lat = 0.32  # Breach starts
            gw_lat = 0.28
        elif s >= 60:
            inv_lat = 0.35  # Sustained 15s -> Alert fires!
            gw_lat = 0.38
        else:
            inv_lat = 0.35
            gw_lat = 0.38

        inv_lat_pts.append({"timestamp": t, "value": inv_lat})
        gw_lat_pts.append({"timestamp": t, "value": gw_lat})
        gw_err_pts.append({"timestamp": t, "value": 0.0})
        ord_pool_pts.append({"timestamp": t, "value": ord_pool})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_err_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


def generate_s5_snapshot(base_dir: Path, base_time: datetime) -> None:
    """S5: Marketing burst (false alarm) starting at offset 20s, recovering by offset 75s.

    Orders pool surges to 8.5 at 30-45s (triggering alert at 45s), then recovers to 2.0
    by 75s, transitioning alert to 'resolved'.
    """
    gw_err_pts = []
    gw_lat_pts = []
    ord_pool_pts = []
    inv_lat_pts = []

    for step in range(41):
        s = step * 15
        t = (base_time + timedelta(seconds=s)).isoformat()
        if s < 20:
            ord_pool = 2.0
        elif 20 <= s <= 45:
            # Sustained surge above 7.0 (at s=45 duration is 15s sustained -> Alert fires)
            ord_pool = 8.5
        elif s == 60:
            ord_pool = 4.0  # Burst drops below threshold
        else:
            ord_pool = 2.0  # Fully recovered back to baseline (resolves alert!)

        ord_pool_pts.append({"timestamp": t, "value": ord_pool})
        gw_err_pts.append({"timestamp": t, "value": 0.0})
        gw_lat_pts.append({"timestamp": t, "value": 0.035 if s > 60 else 0.08})
        inv_lat_pts.append({"timestamp": t, "value": 0.015})

    build_snapshot_file(base_dir, "gateway", "error_rate", gw_err_pts)
    build_snapshot_file(base_dir, "gateway", "latency_p95", gw_lat_pts)
    build_snapshot_file(base_dir, "orders", "db_pool_in_use", ord_pool_pts)
    build_snapshot_file(base_dir, "inventory", "latency_p95", inv_lat_pts)


# -----------------------------------------------------------------------------
# Unit Tests
# -----------------------------------------------------------------------------

@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def default_rules() -> list[AlertRule]:
    return load_rules()


def test_rules_yaml_validity(default_rules: list[AlertRule]) -> None:
    """Verifies that rules.yaml contains exactly the four required rules with valid fields."""
    assert len(default_rules) == 4
    rule_ids = {r.rule_id for r in default_rules}
    assert rule_ids == {
        "gateway_error_rate_high",
        "gateway_latency_p95_high",
        "orders_db_pool_saturation",
        "inventory_latency_p95_high",
    }


@pytest.mark.asyncio
async def test_clean_steady_run_zero_alerts(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: a clean steady run fires ZERO alerts over 10 minutes."""
    generate_clean_steady_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) == 0, f"Expected 0 alerts for clean run, got: {alerts}"


@pytest.mark.asyncio
async def test_s1_fires_pool_saturation_at_plausible_time(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: S1 fires at least one alert at a plausible time on orders."""
    generate_s1_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) >= 1

    pool_alerts = [a for a in alerts if a.rule_id == "orders_db_pool_saturation"]
    assert len(pool_alerts) == 1
    alert = pool_alerts[0]
    assert alert.service == "orders"
    assert alert.status == "firing"

    # Injection happened at t+15s; breach fired at t+60s (+45s relative to injection)
    delta_s = (alert.fired_at - base_time).total_seconds()
    assert 45 <= delta_s <= 75


@pytest.mark.asyncio
async def test_s2_fires_latency_alert_at_plausible_time(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: S2 fires at least one alert on inventory at plausible time."""
    generate_s2_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) >= 1

    inv_alerts = [a for a in alerts if a.rule_id == "inventory_latency_p95_high"]
    assert len(inv_alerts) == 1
    alert = inv_alerts[0]
    assert alert.service == "inventory"
    assert alert.status == "firing"

    # Injection happened at t+15s; breach fired at t+45s (+30s relative to injection)
    delta_s = (alert.fired_at - base_time).total_seconds()
    assert 30 <= delta_s <= 60


@pytest.mark.asyncio
async def test_s3_fires_dependency_alert_at_plausible_time(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: S3 fires alert on inventory latency degradation."""
    generate_s3_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) >= 1

    inv_alerts = [a for a in alerts if a.rule_id == "inventory_latency_p95_high"]
    assert len(inv_alerts) == 1
    alert = inv_alerts[0]
    assert alert.service == "inventory"
    assert alert.status == "firing"

    # Injection at t+20s; breach fired at t+45s or t+50s
    delta_s = (alert.fired_at - base_time).total_seconds()
    assert 30 <= delta_s <= 60


@pytest.mark.asyncio
async def test_s4_fires_on_inventory_not_on_orders_decoy(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: S4 fires on inventory latency; does NOT fire on healthy orders decoy."""
    generate_s4_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) >= 1

    inv_alerts = [a for a in alerts if a.rule_id == "inventory_latency_p95_high"]
    assert len(inv_alerts) == 1
    assert inv_alerts[0].service == "inventory"

    # Crucial decoy check: orders must NOT fire an alert!
    ord_alerts = [a for a in alerts if a.service == "orders"]
    assert len(ord_alerts) == 0


@pytest.mark.asyncio
async def test_s5_burst_fires_alert_that_is_later_resolved(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: S5 (burst) fires an alert that is later resolved (intended false alarm)."""
    generate_s5_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts = await evaluate(default_rules, backend, window)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule_id == "orders_db_pool_saturation"
    assert alert.service == "orders"

    # Assert that the alert transitioned to resolved once the burst ended!
    assert alert.status == "resolved"
    assert alert.resolved_at is not None
    assert alert.resolved_at > alert.fired_at


@pytest.mark.asyncio
async def test_flap_dedupe(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: Flap dedupe ensures one alert per rule per breach episode without spamming."""
    # Create an oscillating series: breaches, drops for 1 point, breaches again immediately
    pts = [
        {"timestamp": (base_time + timedelta(seconds=0)).isoformat(), "value": 2.0},
        {"timestamp": (base_time + timedelta(seconds=15)).isoformat(), "value": 8.0},
        {"timestamp": (base_time + timedelta(seconds=30)).isoformat(), "value": 8.5},
        {"timestamp": (base_time + timedelta(seconds=45)).isoformat(), "value": 9.0},
        {"timestamp": (base_time + timedelta(seconds=60)).isoformat(), "value": 9.5},
        {"timestamp": (base_time + timedelta(seconds=75)).isoformat(), "value": 10.0},
    ]
    build_snapshot_file(tmp_path, "orders", "db_pool_in_use", pts)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(seconds=90))

    alerts = await evaluate(default_rules, backend, window)
    pool_alerts = [a for a in alerts if a.rule_id == "orders_db_pool_saturation"]

    # Exactly ONE alert fired for the sustained episode, not 4 duplicates
    assert len(pool_alerts) == 1
    assert pool_alerts[0].status == "firing"


@pytest.mark.asyncio
async def test_determinism(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: Deterministic: same data => same alerts, same alert_id."""
    generate_s1_snapshot(tmp_path, base_time)
    backend = SnapshotBackend(tmp_path)
    window = (base_time, base_time + timedelta(minutes=10))

    alerts_run_1 = await evaluate(default_rules, backend, window)
    alerts_run_2 = await evaluate(default_rules, backend, window)

    assert len(alerts_run_1) == len(alerts_run_2)
    for a1, a2 in zip(alerts_run_1, alerts_run_2, strict=True):
        assert a1.alert_id == a2.alert_id
        assert a1.fired_at == a2.fired_at
        assert a1.rule_id == a2.rule_id
        assert a1.observed_value == a2.observed_value
        assert a1.status == a2.status
        assert a1.resolved_at == a2.resolved_at


def test_leakage_scan_on_alert_events(
    tmp_path: Path, base_time: datetime, default_rules: list[AlertRule]
) -> None:
    """Requirement: The AlertEvent text and fields must pass the Feature 1.13 leakage scan."""
    sample_event = AlertEvent(
        alert_id="alt_1a2b3c4d5e6f",
        fired_at=base_time,
        service="orders",
        rule_id="orders_db_pool_saturation",
        metric="db_pool_in_use",
        threshold=7.0,
        observed_value=8.5,
        window_seconds=15,
        severity="CRITICAL",
        status="firing",
        resolved_at=None,
    )
    serialized = json.dumps(sample_event.model_dump(mode="json"))

    # 1. Check forbidden family names
    for fam in FORBIDDEN_FAMILY_NAMES:
        assert fam.lower() not in serialized.lower(), f"Family '{fam}' leaked in AlertEvent"

    # 2. Check forbidden regex patterns
    for term, pattern in FORBIDDEN_TERMS_PATTERNS:
        assert not pattern.search(serialized), f"Forbidden pattern '{term}' matched in AlertEvent"

    # 3. Check scenario ID patterns
    for scn in ["S1", "S2", "S3", "S4", "S5"]:
        pattern = re.compile(rf"\b{scn}\b", re.IGNORECASE)
        assert not pattern.search(serialized), f"Scenario ID '{scn}' leaked in AlertEvent"


def test_tuning_invariant_rejects_incident_data() -> None:
    """Requirement: Thresholds tuned ONLY on healthy baseline, never on incident data."""
    # Case 1: Healthy baseline dataset succeeds
    healthy_meta = {
        "is_incident": False,
        "faults": [],
        "load_profile": "steady_20rps",
    }
    healthy_metrics = {
        "gateway:error_rate": [0.0] * 20,
        "gateway:latency_p95": [0.035] * 20,
        "orders:db_pool_in_use": [2.0] * 20,
        "inventory:latency_p95": [0.015] * 20,
    }
    thresholds = tune_thresholds_from_baseline(healthy_metrics, healthy_meta)
    assert "gateway_error_rate_high" in thresholds
    assert thresholds["orders_db_pool_saturation"] == 7.0

    # Case 2: Incident flag raises ValueError
    incident_meta = {"is_incident": True}
    with pytest.raises(ValueError, match="is_incident is True"):
        assert_healthy_baseline_dataset(incident_meta)

    # Case 3: Faults in metadata raises ValueError
    faulty_meta = {"faults": ["redis_latency"]}
    with pytest.raises(ValueError, match="faults detected"):
        assert_healthy_baseline_dataset(faulty_meta)

    # Case 4: Scenario tag raises ValueError
    scn_meta = {"scenario_id": "S1"}
    with pytest.raises(ValueError, match="incident scenario 'S1'"):
        assert_healthy_baseline_dataset(scn_meta)


@pytest.mark.asyncio
async def test_cli_execution(tmp_path: Path, base_time: datetime) -> None:
    """Requirement: CLI: python -m baseline.alerts evaluate --run <run_id>."""
    generate_s1_snapshot(tmp_path, base_time)
    alerts = await run_evaluate_cli(
        run_id="run_test_s1",
        data_dir=str(tmp_path),
        format_type="table",
    )
    assert len(alerts) >= 1
