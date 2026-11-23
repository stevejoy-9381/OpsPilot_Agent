import inspect
import secrets
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger
from lab.common.profiles import VersionProfileRegistry
from lab.common.settings import LabSettings

logger = get_lab_logger("lab.admin")


class DeployRequest(BaseModel):
    version: str = Field(description="Target version profile to activate")


class FaultsUpdateRequest(BaseModel):
    dependency_latency_ms: dict[str, float] | None = None
    dependency_error_rate: dict[str, float] | None = None
    leak_on_conflict: bool | None = None


def create_admin_auth_dependency(settings: LabSettings) -> Callable[[str | None], None]:
    """Creates a FastAPI dependency validating X-Lab-Admin-Token via constant-time compare."""

    def _verify_token(
        x_lab_admin_token: str | None = Header(default=None, alias="X-Lab-Admin-Token"),
    ) -> None:
        expected = settings.lab_admin_token.get_secret_value()
        if not x_lab_admin_token or not secrets.compare_digest(x_lab_admin_token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing lab admin token",
            )

    return _verify_token


def create_admin_router(
    settings: LabSettings,
    fault_state: FaultState,
    version_registry: VersionProfileRegistry,
    on_deploy: Callable[[str], Any | Awaitable[Any]] | None = None,
    on_reset: Callable[[], Any | Awaitable[Any]] | None = None,
) -> APIRouter:
    """Factory creating an authenticated administrative control router for the lab service.
    
    CRITICAL: Admin handlers never write log lines naming injected faults.
    """
    router = APIRouter(
        prefix="/admin",
        tags=["admin"],
        dependencies=[Depends(create_admin_auth_dependency(settings))],
    )

    @router.get("/state")
    async def get_state() -> dict[str, Any]:
        """Returns the active running version, profile flags, and fault status."""
        active_profile = version_registry.get_active_profile()
        fault_snapshot = await fault_state.get_state()
        return {
            "service": settings.service_name,
            "version": version_registry.active_version,
            "profile": active_profile.model_dump(),
            "faults": fault_snapshot,
        }

    @router.post("/deploy")
    async def deploy(payload: DeployRequest) -> dict[str, str]:
        """Switches active version profile and resets pools/in-memory state."""
        if not version_registry.has_version(payload.version):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Version '{payload.version}' is not registered in service profile manifest",
            )

        version_registry.set_active_version(payload.version)

        if on_deploy is not None:
            res = on_deploy(payload.version)
            if inspect.isawaitable(res):
                await res

        # Neutral logging: never name faults or describe bugs
        logger.info(
            event="profile_deployed",
            msg="Service version profile switched",
            target_version=payload.version,
        )

        return {"status": "deployed", "version": payload.version}

    @router.post("/faults")
    async def update_faults(payload: FaultsUpdateRequest) -> dict[str, str]:
        """Updates service fault configuration.
        
        CRITICAL: Never logs details or names of injected faults.
        """
        await fault_state.update_faults(
            dependency_latency_ms=payload.dependency_latency_ms,
            dependency_error_rate=payload.dependency_error_rate,
            leak_on_conflict=payload.leak_on_conflict,
        )

        # Neutral logging: zero fault keywords or parameter disclosure
        logger.info(
            event="config_applied",
            msg="Administrative configuration update applied",
        )

        return {"status": "faults_updated"}

    @router.post("/reset")
    async def reset() -> dict[str, str]:
        """Clears all active faults and resets service state to baseline."""
        await fault_state.reset()

        if version_registry.has_version("v1.0.0"):
            version_registry.set_active_version("v1.0.0")

        if on_reset is not None:
            res = on_reset()
            if inspect.isawaitable(res):
                await res

        logger.info(
            event="state_reset",
            msg="Service state reset to baseline",
        )

        return {"status": "reset"}

    return router
