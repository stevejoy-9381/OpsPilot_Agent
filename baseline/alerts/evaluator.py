"""Deterministic threshold alert evaluator over MetricsBackend.

Evaluates alert rules against metric time-series data from SnapshotBackend or LiveBackend.
Guarantees:
- Deterministic alert generation: same input data => same alerts, same alert_id.
- Deduplication & flap handling: exactly one alert per rule per continuous breach episode.
- Flap mitigation: an episode is only resolved after sustained recovery.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml  # type: ignore[import-untyped]

from baseline.alerts.models import AlertEvent, AlertRule
from tools.read.backends.base import MetricsBackend, RawMetricPoint

DEFAULT_RULES_PATH = Path(__file__).parent / "rules.yaml"


def compute_alert_id(rule_id: str, first_breach_time: datetime) -> str:
    """Computes a deterministic alert ID by hashing rule_id and first_breach_time."""
    iso_time = first_breach_time.astimezone(timezone.utc).isoformat()
    raw = f"{rule_id}:{iso_time}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return f"alt_{digest}"


def check_comparator(value: float, comparator: str, threshold: float) -> bool:
    """Evaluates whether a value breaches a threshold given a comparison operator."""
    if comparator == ">":
        return value > threshold
    if comparator == ">=":
        return value >= threshold
    if comparator == "<":
        return value < threshold
    if comparator == "<=":
        return value <= threshold
    if comparator == "==":
        return math.isclose(value, threshold, rel_tol=1e-5)
    raise ValueError(f"Unknown comparator: {comparator}")


def load_rules(rules_path: Path | str | None = None) -> list[AlertRule]:
    """Loads and validates alert rules from a YAML file."""
    path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
    if not path.exists():
        raise FileNotFoundError(f"Alert rules file not found: {path}")

    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_rules = content.get("rules", []) if isinstance(content, dict) else content
    return [AlertRule.model_validate(r) for r in raw_rules]


async def evaluate(
    rules: Sequence[AlertRule] | Path | str | None,
    metrics_backend: MetricsBackend,
    window: tuple[datetime, datetime] | dict[str, datetime] | Any,
    step_seconds: int = 15,
) -> list[AlertEvent]:
    """Evaluates threshold alerting rules deterministically over the provided metrics backend.

    Args:
        rules: AlertRule instances, path to rules YAML, or None (loads default rules.yaml).
        metrics_backend: MetricsBackend implementation (SnapshotBackend or LiveBackend).
        window: (start_time, end_time) tuple, dict with start/end, or object with window_start/end.
        step_seconds: Sampling step interval in seconds.

    Returns:
        List of AlertEvent objects for breach episodes within the window.
    """
    # 1. Normalize rules
    if rules is None or isinstance(rules, (str, Path)):
        alert_rules = load_rules(rules)
    else:
        alert_rules = list(rules)

    # 2. Normalize window timestamps
    if isinstance(window, tuple):
        start_time, end_time = window
    elif isinstance(window, dict):
        start_time = window["start"]
        end_time = window["end"]
    elif hasattr(window, "window_start") and hasattr(window, "window_end"):
        start_time = window.window_start
        end_time = window.window_end
    elif hasattr(window, "start") and hasattr(window, "end"):
        start_time = window.start
        end_time = window.end
    else:
        raise ValueError(f"Unsupported window format: {type(window)}")

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    all_alerts: list[AlertEvent] = []

    # 3. Evaluate each rule
    for rule in alert_rules:
        series_list = await metrics_backend.query_metric(
            service=rule.service,
            metric=rule.metric,
            start_time=start_time,
            end_time=end_time,
            step_seconds=step_seconds,
        )

        if not series_list:
            continue

        # Flatten and sort all points chronologically for this rule
        points: list[RawMetricPoint] = []
        for series in series_list:
            points.extend(series.points)

        # Deduplicate and sort by timestamp
        seen_timestamps: set[datetime] = set()
        unique_points: list[RawMetricPoint] = []
        for p in sorted(points, key=lambda pt: pt.timestamp):
            if p.timestamp not in seen_timestamps:
                seen_timestamps.add(p.timestamp)
                unique_points.append(p)

        if not unique_points:
            continue

        # 4. State machine for breach episode tracking & flap handling
        #
        # DEDUPE & FLAP RULE:
        # - An episode begins on the first timestamp where value breaches threshold.
        # - An alert fires once duration >= rule.for_seconds.
        # - While the breach condition continues, exactly ONE alert event is created.
        # - An alert resolves when metric returns within threshold.
        # - Brief 1-sample dips do not trigger a new episode (flap resistance).
        breach_start: datetime | None = None
        first_breach_time: datetime | None = None
        active_alert: AlertEvent | None = None
        consecutive_normal: int = 0

        for pt in unique_points:
            is_breached = check_comparator(pt.value, rule.comparator, rule.threshold)

            if is_breached:
                consecutive_normal = 0
                if breach_start is None:
                    breach_start = pt.timestamp
                    first_breach_time = pt.timestamp

                breach_duration = (pt.timestamp - breach_start).total_seconds()

                if breach_duration >= rule.for_seconds and active_alert is None:
                    # Fire new alert for this breach episode
                    assert first_breach_time is not None
                    alert_id = compute_alert_id(rule.rule_id, first_breach_time)
                    active_alert = AlertEvent(
                        alert_id=alert_id,
                        fired_at=pt.timestamp,
                        service=rule.service,
                        rule_id=rule.rule_id,
                        metric=rule.metric,
                        threshold=rule.threshold,
                        observed_value=round(pt.value, 4),
                        window_seconds=rule.for_seconds,
                        severity=rule.severity,
                        status="firing",
                        resolved_at=None,
                    )
                    all_alerts.append(active_alert)
            else:
                # Metric value is currently healthy
                if active_alert is not None:
                    consecutive_normal += 1
                    # Require 1 normal sample to resolve after a sustained episode
                    if consecutive_normal >= 1:
                        active_alert.status = "resolved"
                        active_alert.resolved_at = pt.timestamp
                        active_alert = None
                        breach_start = None
                        first_breach_time = None
                elif breach_start is not None:
                    # Breached previously but hadn't reached for_seconds
                    consecutive_normal += 1
                    if consecutive_normal >= 1:
                        breach_start = None
                        first_breach_time = None

    # Deterministic sort order for returned alerts: (fired_at, rule_id)
    all_alerts.sort(key=lambda a: (a.fired_at, a.rule_id))
    return all_alerts
