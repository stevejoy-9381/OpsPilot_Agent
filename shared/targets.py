"""Lab target allowlist registry and TargetStateReader protocol.

config/lab_targets.yaml is the single source of truth shared by:
- Feature 7 (Policy Validator target allowlist P4 & state reader)
- Feature 9 (Execution Broker target allowlist)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.actions import ActionType

DEFAULT_LAB_TARGETS_PATH: Path = Path(__file__).resolve().parent.parent / "config" / "lab_targets.yaml"
ALLOWED_TARGET_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.ROLLBACK_SERVICE, ActionType.RESTART_SERVICE}
)


class TargetSpec(BaseModel):
    """Single allowlisted lab target entry from config/lab_targets.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    kind: Literal["lab_service"] = "lab_service"
    allowed_actions: tuple[ActionType, ...] = Field(min_length=1)
    owner: Literal["lab"] = "lab"

    @field_validator("target_id", "service")
    @classmethod
    def normalize_non_empty(cls, v: str, info: Any) -> str:
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned

    @field_validator("allowed_actions")
    @classmethod
    def validate_allowed_actions(cls, v: tuple[ActionType, ...]) -> tuple[ActionType, ...]:
        for act in v:
            if act not in ALLOWED_TARGET_ACTIONS:
                raise ValueError(
                    f"Action '{act.value}' is not permitted on lab targets; "
                    f"allowed: {[a.value for a in sorted(ALLOWED_TARGET_ACTIONS, key=lambda x: x.value)]}"
                )
        return v


class TargetRegistry(BaseModel):
    """Validated 1:1 registry mapping between target_id and service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: tuple[TargetSpec, ...]
    by_target_id: dict[str, TargetSpec] = Field(default_factory=dict)
    by_service: dict[str, TargetSpec] = Field(default_factory=dict)

    @classmethod
    def from_specs(cls, specs: list[TargetSpec]) -> TargetRegistry:
        if not specs:
            raise ValueError("Target registry cannot be empty")
        by_target: dict[str, TargetSpec] = {}
        by_service: dict[str, TargetSpec] = {}
        for spec in specs:
            if spec.target_id in by_target:
                raise ValueError(f"Duplicate target_id in registry: '{spec.target_id}'")
            if spec.service in by_service:
                raise ValueError(
                    f"Duplicate service in registry: '{spec.service}' "
                    f"(each target must map 1:1 to one service)"
                )
            by_target[spec.target_id] = spec
            by_service[spec.service] = spec
        return cls(targets=tuple(specs), by_target_id=by_target, by_service=by_service)

    def get_by_target_id(self, target_id: str) -> TargetSpec | None:
        return self.by_target_id.get(target_id.strip().lower())

    def get_by_service(self, service: str) -> TargetSpec | None:
        return self.by_service.get(service.strip().lower())


def load_target_registry(path: Path | str | None = None) -> TargetRegistry:
    """Loads and validates config/lab_targets.yaml."""
    target_path = Path(path) if path is not None else DEFAULT_LAB_TARGETS_PATH
    raw = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "targets" not in raw or not isinstance(raw["targets"], list):
        raise ValueError(f"Invalid target registry YAML structure in {target_path}")
    specs = [TargetSpec(**entry) for entry in raw["targets"]]
    return TargetRegistry.from_specs(specs)


class TargetStateErrorCode(str, Enum):
    """Typed non-exception errors returned by TargetStateReader."""

    TARGET_NOT_FOUND = "target_not_found"
    STATE_UNAVAILABLE = "state_unavailable"
    STALE_STATE = "stale_state"


class TargetState(BaseModel):
    """Read-only snapshot of a lab target's current deployment and action state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    current_version: str
    previous_version: str | None = None
    last_action_at: datetime | None = None
    source: str
    read_at: datetime

    @field_validator("last_action_at", "read_at", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, str):
            raw = v.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        elif isinstance(v, datetime):
            dt = v
        else:
            raise ValueError(f"Invalid datetime value: {v!r}")
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


class TargetStateError(BaseModel):
    """Typed error returned when target state cannot be read or is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: TargetStateErrorCode
    target_id: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class TargetStateReader(Protocol):
    """Read-only protocol for inspecting a lab target's current state."""

    def current_state(
        self,
        target_id: str,
        as_of: datetime,
    ) -> TargetState | TargetStateError:
        """Returns TargetState or a typed TargetStateError; never raises."""
        ...


@runtime_checkable
class BrokerTargetCheckPort(Protocol):
    """Pre-flight stale-target check interface for the Execution Broker (Feature 9).

    ApprovalService repeats target-state validation at approval time via TargetStateReader,
    and the Broker repeats this pre-flight check at dispatch time immediately prior to
    mutating the target container/service to detect drift between approval and execution.
    """

    def verify_target_precondition(
        self,
        target_id: str,
        expected_version: str,
        as_of: datetime,
    ) -> TargetState | TargetStateError:
        """Verifies live target version matches expected_version at dispatch time."""
        ...
