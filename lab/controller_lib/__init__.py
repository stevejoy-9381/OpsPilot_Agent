"""OpsPilot Lab Controller Library: Deployment management and historical audit logging."""

from __future__ import annotations

from lab.controller_lib.deploy import (
    DeployRecord,
    deploy,
    get_deploy_history,
    rollback,
    seed_deploy_history,
)
from lab.controller_lib.metadata import VersionMetadata, get_version_metadata

__all__ = [
    "DeployRecord",
    "VersionMetadata",
    "deploy",
    "get_deploy_history",
    "get_version_metadata",
    "rollback",
    "seed_deploy_history",
]
