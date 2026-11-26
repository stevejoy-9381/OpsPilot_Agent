import asyncio
import inspect
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from lab.common.faults import FaultState
from lab.common.logging import get_lab_logger
from lab.common.profiles import VersionProfileRegistry
from lab.common.settings import LabSettings
from lab.controller_lib.deploy import record_deploy_in_tx

logger = get_lab_logger("lab.admin")


class DeployRequest(BaseModel):
    version: str = Field(description="Target version profile to activate")


class FaultsUpdateRequest(BaseModel):
    dependency_latency_ms: dict[str, float] | None = None
    dependency_error_rate: dict[str, float] | None = None
    leak_on_conflict: bool | None = None


class RollbackActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1, max_length=128)
    expected_current_version: str = Field(min_length=1, max_length=64)
    to_version: str = Field(min_length=1, max_length=64)


class RestartActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1, max_length=128)
    expected_current_version: str = Field(min_length=1, max_length=64)


class TestHooksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delay_before_response_ms: int = Field(default=0, ge=0, le=30000)
    drop_response_after_apply: bool = False
    fail_before_apply: bool = False
    fail_between_deploy_and_record: bool = False


@dataclass
class AdminTestHooks:
    """Test-only failure injection hooks (inert unless LAB_TEST_HOOKS_ENABLED=true)."""

    delay_before_response_ms: int = 0
    drop_response_after_apply: bool = False
    fail_before_apply: bool = False
    fail_between_deploy_and_record: bool = False


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


def _normalize_pg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def create_admin_router(
    settings: LabSettings,
    fault_state: FaultState,
    version_registry: VersionProfileRegistry,
    on_deploy: Callable[[str], Any | Awaitable[Any]] | None = None,
    on_reset: Callable[[], Any | Awaitable[Any]] | None = None,
    on_restart: Callable[[], Any | Awaitable[Any]] | None = None,
    admin_db_url: str | None = None,
) -> APIRouter:
    """Factory creating an authenticated administrative control router for the lab service.

    CRITICAL: Admin handlers never write log lines naming injected faults, brokers,
    action_keys, or incidents.
    """
    router = APIRouter(
        prefix="/admin",
        tags=["admin"],
        dependencies=[Depends(create_admin_auth_dependency(settings))],
    )

    action_lock = asyncio.Lock()
    db_url = _normalize_pg_url(admin_db_url or settings.admin_database_url)
    hooks = AdminTestHooks()
    restart_state: dict[str, Any] = {
        "restart_count": 0,
        "last_restarted_at": None,
    }

    async def _invoke_callback(cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is not None:
            res = cb(*args)
            if inspect.isawaitable(res):
                await res

    def _fetch_applied_action(
        cur: psycopg.Cursor[Any], action_key: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        sql_str = """
            SELECT action_key, service, action, from_version, to_version, applied_at, result
            FROM lab.applied_actions
            WHERE action_key = %s
        """
        if for_update:
            sql_str += " FOR UPDATE"
        cur.execute(sql_str, (action_key,))
        row = cur.fetchone()
        if row is None:
            return None
        result_val = row[6] if isinstance(row[6], dict) else json.loads(row[6])
        return {
            "action_key": row[0],
            "service": row[1],
            "action": row[2],
            "from_version": row[3],
            "to_version": row[4],
            "applied_at": row[5].astimezone(timezone.utc).isoformat()
            if isinstance(row[5], datetime)
            else str(row[5]),
            "result": result_val,
        }

    async def _apply_post_hooks() -> None:
        if not settings.lab_test_hooks_enabled:
            return
        if hooks.delay_before_response_ms > 0:
            await asyncio.sleep(hooks.delay_before_response_ms / 1000.0)
        if hooks.drop_response_after_apply:
            raise ConnectionResetError("Simulated connection drop after apply")

    @router.get("/state")
    async def get_state() -> dict[str, Any]:
        """Returns the active running version, profile flags, fault status, and restart info."""
        active_profile = version_registry.get_active_profile()
        fault_snapshot = await fault_state.get_state()
        return {
            "service": settings.service_name,
            "version": version_registry.active_version,
            "profile": active_profile.model_dump(),
            "faults": fault_snapshot,
            "restart_count": restart_state["restart_count"],
            "last_restarted_at": restart_state["last_restarted_at"],
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
        await _invoke_callback(on_deploy, payload.version)

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

        await _invoke_callback(on_reset)

        logger.info(
            event="state_reset",
            msg="Service state reset to baseline",
        )

        return {"status": "reset"}

    @router.post("/actions/rollback")
    async def action_rollback(payload: RollbackActionRequest) -> Response:
        """Compare-and-set idempotent rollback using controller_lib in one DB transaction."""
        async with action_lock:
            if settings.lab_test_hooks_enabled and hooks.fail_before_apply:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable before apply",
                )

            if not version_registry.has_version(payload.to_version):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Version '{payload.to_version}' is not registered in service profile manifest",
                )

            with psycopg.connect(db_url, autocommit=False) as conn:
                with conn.cursor() as cur:
                    existing = _fetch_applied_action(cur, payload.action_key, for_update=True)
                    if existing is not None:
                        conn.rollback()
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "status": "already_applied",
                                "service": existing["service"],
                                "action": existing["action"],
                                "from_version": existing["from_version"],
                                "to_version": existing["to_version"],
                                "applied_at": existing["applied_at"],
                                "result": existing["result"],
                            },
                        )

                    current_version = version_registry.active_version
                    if current_version != payload.expected_current_version:
                        conn.rollback()
                        return JSONResponse(
                            status_code=status.HTTP_409_CONFLICT,
                            content={
                                "status": "precondition_failed",
                                "service": settings.service_name,
                                "action": "rollback_service",
                                "expected_current_version": payload.expected_current_version,
                                "current_version": current_version,
                            },
                        )

                    previous_version = current_version
                    try:
                        # 1. Insert lab.deploys row in the same transaction via controller_lib
                        deploy_record = record_deploy_in_tx(
                            conn,
                            service=settings.service_name,
                            old_version=previous_version,
                            new_version=payload.to_version,
                            author="release-automation@internal",
                        )

                        # Test hook: simulate crash/failure between lab.deploys and lab.applied_actions
                        if (
                            settings.lab_test_hooks_enabled
                            and hooks.fail_between_deploy_and_record
                        ):
                            raise RuntimeError("Simulated failure between deploy and record")

                        # 2. Switch in-process version profile and drain/reset pools
                        version_registry.set_active_version(payload.to_version)
                        await _invoke_callback(on_deploy, payload.to_version)

                        now_iso = datetime.now(timezone.utc).isoformat()
                        result_payload = {
                            "from_version": previous_version,
                            "to_version": payload.to_version,
                            "deploy_id": deploy_record.deploy_id,
                            "applied_at": now_iso,
                        }

                        # 3. Record in lab.applied_actions in the same transaction
                        cur.execute(
                            """
                            INSERT INTO lab.applied_actions (
                                action_key, service, action, from_version, to_version, result
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (action_key) DO NOTHING
                            RETURNING applied_at
                            """,
                            (
                                payload.action_key,
                                settings.service_name,
                                "rollback_service",
                                previous_version,
                                payload.to_version,
                                json.dumps(result_payload),
                            ),
                        )
                        inserted = cur.fetchone()
                        if inserted is None:
                            conn.rollback()
                            version_registry.set_active_version(previous_version)
                            stored = _fetch_applied_action(cur, payload.action_key)
                            assert stored is not None
                            return JSONResponse(
                                status_code=status.HTTP_200_OK,
                                content={
                                    "status": "already_applied",
                                    "service": stored["service"],
                                    "action": stored["action"],
                                    "from_version": stored["from_version"],
                                    "to_version": stored["to_version"],
                                    "applied_at": stored["applied_at"],
                                    "result": stored["result"],
                                },
                            )

                        conn.commit()
                    except Exception:
                        conn.rollback()
                        version_registry.set_active_version(previous_version)
                        raise

            logger.info(
                event="profile_deployed",
                msg="Service version profile switched",
                target_version=payload.to_version,
            )

            await _apply_post_hooks()

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "applied",
                    "service": settings.service_name,
                    "action": "rollback_service",
                    "from_version": previous_version,
                    "to_version": payload.to_version,
                    "applied_at": result_payload["applied_at"],
                    "result": result_payload,
                },
            )

    @router.post("/actions/restart")
    async def action_restart(payload: RestartActionRequest) -> Response:
        """Compare-and-set idempotent in-process restart (Option A semantics)."""
        async with action_lock:
            if settings.lab_test_hooks_enabled and hooks.fail_before_apply:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service temporarily unavailable before apply",
                )

            with psycopg.connect(db_url, autocommit=False) as conn:
                with conn.cursor() as cur:
                    existing = _fetch_applied_action(cur, payload.action_key, for_update=True)
                    if existing is not None:
                        conn.rollback()
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content={
                                "status": "already_applied",
                                "service": existing["service"],
                                "action": existing["action"],
                                "from_version": existing["from_version"],
                                "to_version": existing["to_version"],
                                "applied_at": existing["applied_at"],
                                "result": existing["result"],
                            },
                        )

                    current_version = version_registry.active_version
                    if current_version != payload.expected_current_version:
                        conn.rollback()
                        return JSONResponse(
                            status_code=status.HTTP_409_CONFLICT,
                            content={
                                "status": "precondition_failed",
                                "service": settings.service_name,
                                "action": "restart_service",
                                "expected_current_version": payload.expected_current_version,
                                "current_version": current_version,
                            },
                        )

                    try:
                        # Option A: reset pools/caches/client state AND clear dependency client faults,
                        # while preserving active version and version-profile bugs (leak_on_conflict).
                        await _invoke_callback(on_restart)
                        await fault_state.reset_dependency_client_state()

                        restart_state["restart_count"] += 1
                        now_iso = datetime.now(timezone.utc).isoformat()
                        restart_state["last_restarted_at"] = now_iso

                        result_payload = {
                            "from_version": current_version,
                            "to_version": current_version,
                            "restart_count": restart_state["restart_count"],
                            "restarted_at": now_iso,
                        }

                        cur.execute(
                            """
                            INSERT INTO lab.applied_actions (
                                action_key, service, action, from_version, to_version, result
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (action_key) DO NOTHING
                            RETURNING applied_at
                            """,
                            (
                                payload.action_key,
                                settings.service_name,
                                "restart_service",
                                current_version,
                                current_version,
                                json.dumps(result_payload),
                            ),
                        )
                        inserted = cur.fetchone()
                        if inserted is None:
                            conn.rollback()
                            stored = _fetch_applied_action(cur, payload.action_key)
                            assert stored is not None
                            return JSONResponse(
                                status_code=status.HTTP_200_OK,
                                content={
                                    "status": "already_applied",
                                    "service": stored["service"],
                                    "action": stored["action"],
                                    "from_version": stored["from_version"],
                                    "to_version": stored["to_version"],
                                    "applied_at": stored["applied_at"],
                                    "result": stored["result"],
                                },
                            )

                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise

            logger.info(
                event="service_restarted",
                msg="Service client state restarted",
                service=settings.service_name,
                version=current_version,
                restart_count=restart_state["restart_count"],
            )

            await _apply_post_hooks()

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "applied",
                    "service": settings.service_name,
                    "action": "restart_service",
                    "from_version": current_version,
                    "to_version": current_version,
                    "applied_at": result_payload["restarted_at"],
                    "result": result_payload,
                },
            )

    @router.get("/actions/{action_key}")
    async def get_action_status(action_key: str) -> dict[str, Any]:
        """Queries lab.applied_actions for reconciliation by action_key."""
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                existing = _fetch_applied_action(cur, action_key)
                if existing is None:
                    return {
                        "status": "not_applied",
                    }
                return {
                    "status": "applied",
                    "service": existing["service"],
                    "action": existing["action"],
                    "from_version": existing["from_version"],
                    "to_version": existing["to_version"],
                    "applied_at": existing["applied_at"],
                    "result": existing["result"],
                }

    @router.post("/test-hooks")
    async def configure_test_hooks(payload: TestHooksRequest) -> dict[str, Any]:
        """Configures failure-injection hooks for tests ONLY (404 when disabled)."""
        if not settings.lab_test_hooks_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
            )
        hooks.delay_before_response_ms = payload.delay_before_response_ms
        hooks.drop_response_after_apply = payload.drop_response_after_apply
        hooks.fail_before_apply = payload.fail_before_apply
        hooks.fail_between_deploy_and_record = payload.fail_between_deploy_and_record
        return {
            "status": "configured",
            "delay_before_response_ms": hooks.delay_before_response_ms,
            "drop_response_after_apply": hooks.drop_response_after_apply,
            "fail_before_apply": hooks.fail_before_apply,
            "fail_between_deploy_and_record": hooks.fail_between_deploy_and_record,
        }

    return router
