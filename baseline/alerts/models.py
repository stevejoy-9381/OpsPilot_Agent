"""Data models for deterministic alerting rules and evaluated alert events."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Comparator = Literal[">", ">=", "<", "<=", "=="]
Severity = Literal["CRITICAL", "WARNING"]
AlertStatus = Literal["firing", "resolved"]


class AlertRule(BaseModel):
    """Declarative specification for a metric threshold alert rule."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="Unique rule identifier")
    service: str = Field(description="Target microservice (e.g. gateway, orders, inventory)")
    metric: str = Field(description="Metric name from standard tool metric enum")
    comparator: Comparator = Field(description="Comparison operator (>, >=, <, <=, ==)")
    threshold: float = Field(description="Numeric boundary value")
    for_seconds: int = Field(
        ge=0,
        description="Duration the breach must be sustained before firing (seconds)",
    )
    severity: Severity = Field(description="Alert severity level: CRITICAL or WARNING")
    description: str = Field(default="", description="Human-readable rule intent")


class AlertEvent(BaseModel):
    """Evaluation output representing a fired or resolved threshold alert."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(
        description="Deterministic identifier: sha256(rule_id + first_breach_time)[:12]"
    )
    fired_at: datetime = Field(
        description="UTC timestamp when sustained breach condition was reached"
    )
    service: str = Field(description="Target microservice where metric threshold was breached")
    rule_id: str = Field(description="Identifier of the rule that fired")
    metric: str = Field(description="Metric measurement identifier")
    threshold: float = Field(description="Configured boundary value")
    observed_value: float = Field(description="Observed metric value at firing time")
    window_seconds: int = Field(
        description="Sustained observation window duration required by rule"
    )
    severity: Severity = Field(description="Alert severity: CRITICAL or WARNING")
    status: AlertStatus = Field(
        default="firing",
        description="Current state of this breach episode: 'firing' or 'resolved'",
    )
    resolved_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when metric returned within threshold bounds",
    )
