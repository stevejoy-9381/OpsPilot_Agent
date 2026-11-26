"""OpsPilot Repositories package (lazy exports to avoid eager agent imports)."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BaseRepository",
    "ExecutionRepository",
    "HypothesisRepository",
    "RunRepository",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BaseRepository": ("apps.api.repositories.base", "BaseRepository"),
    "ExecutionRepository": ("apps.api.repositories.execution_repository", "ExecutionRepository"),
    "HypothesisRepository": ("apps.api.repositories.hypothesis_repository", "HypothesisRepository"),
    "RunRepository": ("apps.api.repositories.run_repository", "RunRepository"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    return getattr(import_module(module_name), attr_name)
