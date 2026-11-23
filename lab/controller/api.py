"""Harness API router and FastAPI application for OpsPilot Lab Controller.

Provides token-protected scenario orchestration and ground truth management.
CRITICAL: The agent NEVER receives the HARNESS_TOKEN. Truth endpoints are strictly isolated.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status

from lab.controller.models import (
    OracleResult,
    RunRequest,
    RunResponse,
    TruthRecord,
)
from lab.controller.oracle import Oracle
from lab.controller.scenarios import get_scenario, list_scenarios
from lab.controller.service import ControllerService
from lab.controller.settings import ControllerSettings
from lab.controller.vault import Vault


def create_controller_app(
    settings: ControllerSettings | None = None,
    service: ControllerService | None = None,
    oracle: Oracle | None = None,
    vault: Vault | None = None,
) -> FastAPI:
    """Factory creating the FastAPI Controller application."""
    ctrl_settings = settings or ControllerSettings()
    ctrl_vault = vault or Vault(ctrl_settings.vault_db_path)
    ctrl_service = service or ControllerService(settings=ctrl_settings, vault=ctrl_vault)
    ctrl_oracle = oracle or Oracle(prometheus_url=ctrl_settings.prometheus_url)

    app = FastAPI(
        title="OpsPilot Lab Controller",
        version=ctrl_settings.version,
        description="Isolated scenario orchestration and ground truth vault on lab-admin.",
    )

    # -------------------------------------------------------------------------
    # Authentication Dependencies
    # -------------------------------------------------------------------------
    def _extract_token(request: Request) -> str | None:
        token = request.headers.get("x-harness-token") or request.headers.get("x-controller-token")
        if not token:
            auth = request.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        return token

    def verify_controller_or_harness_token(request: Request) -> str:
        """Allows access if valid CONTROLLER_TOKEN or HARNESS_TOKEN is provided."""
        token = _extract_token(request)
        c_expected = ctrl_settings.controller_token.get_secret_value()
        h_expected = ctrl_settings.harness_token.get_secret_value()

        if token and (
            secrets.compare_digest(token, c_expected)
            or secrets.compare_digest(token, h_expected)
        ):
            return token

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: valid controller or harness token required",
        )

    def verify_harness_token_only(request: Request) -> str:
        """Strictly requires HARNESS_TOKEN. Controllers/agents are rejected."""
        token = _extract_token(request)
        h_expected = ctrl_settings.harness_token.get_secret_value()
        if token and secrets.compare_digest(token, h_expected):
            return token

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: access to ground truth requires harness token",
        )

    # -------------------------------------------------------------------------
    # Endpoints
    # -------------------------------------------------------------------------
    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "controller"}

    @app.get("/scenarios", tags=["scenarios"])
    async def get_scenarios(
        _: str = Depends(verify_controller_or_harness_token),
    ) -> list[dict[str, str]]:
        """Lists scenario IDs and descriptions (safe metadata without secret root cause)."""
        return [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "description": s.description,
                "family": s.family,
                "split_tag": s.split_tag,
            }
            for s in list_scenarios()
        ]

    @app.post("/runs", response_model=RunResponse, tags=["runs"])
    async def create_run(
        payload: RunRequest,
        _: str = Depends(verify_controller_or_harness_token),
    ) -> RunResponse:
        """Initiates a scenario run and returns run_id only without disclosing truth."""
        try:
            get_scenario(payload.scenario_id)
        except KeyError as err:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Scenario '{payload.scenario_id}' not found",
            ) from err

        run_id = await ctrl_service.start_run(
            scenario_id=payload.scenario_id,
            seed=payload.seed,
            warmup_s=payload.warmup_s,
        )

        # CRITICAL: Returns run_id only; never discloses truth
        return RunResponse(run_id=run_id, scenario_id=payload.scenario_id, status="running")

    @app.get("/runs/{run_id}/truth", response_model=TruthRecord, tags=["truth"])
    async def get_run_truth(
        run_id: str,
        _: str = Depends(verify_harness_token_only),
    ) -> TruthRecord:
        """Retrieves ground truth for a run. ALLOWED ONLY WITH HARNESS_TOKEN."""
        truth = ctrl_service.get_truth(run_id)
        if not truth:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run '{run_id}' not found in vault",
            )
        return truth

    @app.post("/runs/{run_id}/stop", tags=["runs"])
    async def stop_run(
        run_id: str,
        _: str = Depends(verify_controller_or_harness_token),
    ) -> dict[str, str]:
        """Halts an active run and cancels background load generation."""
        stopped = await ctrl_service.stop_run(run_id)
        if not stopped:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Active run '{run_id}' not found or already stopped",
            )
        return {"status": "stopped", "run_id": run_id}

    @app.post("/world/reset", tags=["world"])
    async def reset_world(
        _: str = Depends(verify_controller_or_harness_token),
    ) -> dict[str, Any]:
        """Idempotent world reset: faults cleared, baseline profiles restored."""
        result = await ctrl_service.reset_world()
        return result

    @app.get("/oracle/health", response_model=OracleResult, tags=["oracle"])
    async def get_oracle_health(
        scenario_id: str = Query(..., description="Target scenario identifier"),
        window: str = Query(default="1m", description="Prometheus rate aggregation window"),
        _: str = Depends(verify_harness_token_only),
    ) -> OracleResult:
        """Queries Prometheus to evaluate health against scenario thresholds. Graders only."""
        try:
            scenario = get_scenario(scenario_id)
        except KeyError as err:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Scenario '{scenario_id}' not found",
            ) from err

        res = await ctrl_oracle.oracle_health(scenario.health_thresholds, window=window)
        return res

    # Store references on app state for testing inspection
    app.state.settings = ctrl_settings
    app.state.service = ctrl_service
    app.state.vault = ctrl_vault
    app.state.oracle = ctrl_oracle

    return app


# Default ASGI entrypoint
app = create_controller_app()
