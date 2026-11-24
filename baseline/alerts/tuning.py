"""Tuning utility for baseline alert thresholds.

ENFORCEMENT INVARIANT:
Thresholds are tuned ONLY against healthy baseline traffic (steady 20 RPS healthy run),
never against incident data. This module enforces this constraint programmatically.
"""

from __future__ import annotations

import statistics
from typing import Any


def assert_healthy_baseline_dataset(metadata: dict[str, Any]) -> None:
    """Verifies that a dataset is certified as a healthy baseline run.

    Raises ValueError if any incident flag, fault injection marker, or failure
    family signature is detected.
    """
    if metadata.get("is_incident", False):
        raise ValueError("Cannot tune alert thresholds against incident data: is_incident is True.")

    if metadata.get("faults") or metadata.get("faults_injected", 0) > 0:
        raise ValueError(
            "Cannot tune alert thresholds against incident data: faults detected in metadata."
        )

    # Reject known scenario tags if passed
    scenario_tag = str(metadata.get("scenario_id", "")).upper()
    if scenario_tag in ("S1", "S2", "S3", "S4", "S5", "F1", "F2", "F3", "F4", "F5"):
        raise ValueError(
            f"Cannot tune alert thresholds against incident scenario '{scenario_tag}'. "
            "Thresholds must be derived exclusively from healthy baseline runs."
        )

    profile = metadata.get("profile", metadata.get("load_profile", ""))
    if "burst" in profile.lower() or "spike" in profile.lower() or "fault" in profile.lower():
        raise ValueError(
            f"Load profile '{profile}' is non-baseline. "
            "Only steady healthy baseline traffic can be used for tuning."
        )


def tune_thresholds_from_baseline(
    baseline_metrics: dict[str, list[float]],
    metadata: dict[str, Any],
) -> dict[str, float]:
    """Computes recommended alert thresholds strictly from healthy baseline metric samples.

    Args:
        baseline_metrics: Dict mapping "service:metric" to lists of healthy float values.
        metadata: Run metadata dictionary proving the run is healthy baseline traffic.

    Returns:
        Dict mapping rule_id to recommended threshold.
    """
    assert_healthy_baseline_dataset(metadata)

    thresholds: dict[str, float] = {}

    # 1. Gateway Error Rate: baseline is 0.0 -> threshold at 0.05 (5%)
    gw_err = baseline_metrics.get("gateway:error_rate", [0.0])
    gw_err_max = max(gw_err) if gw_err else 0.0
    thresholds["gateway_error_rate_high"] = max(0.05, gw_err_max + 0.05)

    # 2. Gateway Latency p95: baseline ~0.035s -> threshold at 0.25s (~5x baseline)
    gw_lat = baseline_metrics.get("gateway:latency_p95", [0.035])
    gw_lat_p95 = statistics.quantiles(gw_lat, n=20)[-1] if len(gw_lat) >= 20 else max(gw_lat)
    thresholds["gateway_latency_p95_high"] = max(0.25, round(gw_lat_p95 * 5.0, 2))

    # 3. Orders DB Pool Saturation: baseline ~2.0 connections -> threshold at 7.0 (70% of 10)
    ord_pool = baseline_metrics.get("orders:db_pool_in_use", [2.0])
    ord_pool_max = max(ord_pool) if ord_pool else 2.0
    thresholds["orders_db_pool_saturation"] = max(7.0, ord_pool_max + 5.0)

    # 4. Inventory Latency p95: baseline ~0.015s -> threshold at 0.15s (150ms timeout)
    inv_lat = baseline_metrics.get("inventory:latency_p95", [0.015])
    inv_lat_p95 = statistics.quantiles(inv_lat, n=20)[-1] if len(inv_lat) >= 20 else max(inv_lat)
    thresholds["inventory_latency_p95_high"] = max(0.15, round(inv_lat_p95 * 5.0, 2))

    return thresholds
