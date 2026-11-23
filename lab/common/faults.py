import asyncio
from typing import Any

from pydantic import BaseModel, Field


class FaultStateSnapshot(BaseModel):
    """Pydantic model snapshot of current active faults."""

    dependency_latency_ms: dict[str, float] = Field(default_factory=dict)
    dependency_error_rate: dict[str, float] = Field(default_factory=dict)
    leak_on_conflict: bool = False


class FaultState:
    """Async-safe state container managing active fault injections.
    
    All faults default to off / zero upon initialization.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._dependency_latency_ms: dict[str, float] = {}
        self._dependency_error_rate: dict[str, float] = {}
        self._leak_on_conflict: bool = False

    async def get_dependency_latency(self, dependency: str) -> float:
        """Returns the artificial latency in milliseconds for a dependency (default: 0.0)."""
        async with self._lock:
            return self._dependency_latency_ms.get(dependency, 0.0)

    async def get_dependency_error_rate(self, dependency: str) -> float:
        """Returns the artificial error rate [0.0, 1.0] for a dependency (default: 0.0)."""
        async with self._lock:
            return self._dependency_error_rate.get(dependency, 0.0)

    async def should_leak_on_conflict(self) -> bool:
        """Returns whether database connection leakage on conflict is active."""
        async with self._lock:
            return self._leak_on_conflict

    async def set_dependency_latency(self, dependency: str, latency_ms: float) -> None:
        async with self._lock:
            self._dependency_latency_ms[dependency] = max(0.0, latency_ms)

    async def set_dependency_error_rate(self, dependency: str, rate: float) -> None:
        async with self._lock:
            self._dependency_error_rate[dependency] = max(0.0, min(1.0, rate))

    async def set_leak_on_conflict(self, enabled: bool) -> None:
        async with self._lock:
            self._leak_on_conflict = enabled

    async def update_faults(
        self,
        dependency_latency_ms: dict[str, float] | None = None,
        dependency_error_rate: dict[str, float] | None = None,
        leak_on_conflict: bool | None = None,
    ) -> None:
        """Updates multiple fault parameters under a single lock."""
        async with self._lock:
            if dependency_latency_ms is not None:
                self._dependency_latency_ms.update(dependency_latency_ms)
            if dependency_error_rate is not None:
                self._dependency_error_rate.update(dependency_error_rate)
            if leak_on_conflict is not None:
                self._leak_on_conflict = leak_on_conflict

    async def reset(self) -> None:
        """Resets all fault flags back to clean default state."""
        async with self._lock:
            self._dependency_latency_ms.clear()
            self._dependency_error_rate.clear()
            self._leak_on_conflict = False

    async def get_state(self) -> dict[str, Any]:
        """Returns an immutable dictionary snapshot of the current fault state."""
        async with self._lock:
            return {
                "dependency_latency_ms": dict(self._dependency_latency_ms),
                "dependency_error_rate": dict(self._dependency_error_rate),
                "leak_on_conflict": self._leak_on_conflict,
            }
