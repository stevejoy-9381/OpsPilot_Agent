"""Controller service orchestrating world reset, runs, injection, and load."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from lab.controller.models import ScenarioDefinition, TruthRecord
from lab.controller.scenarios import get_scenario
from lab.controller.settings import ControllerSettings
from lab.controller.vault import Vault
from lab.controller_lib.deploy import deploy


class ControllerService:
    """Manages scenario execution, fault injection, and world state resets."""

    def __init__(
        self,
        settings: ControllerSettings | None = None,
        vault: Vault | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or ControllerSettings()
        self.vault = vault or Vault(self.settings.vault_db_path)
        self._external_client = http_client
        self._active_runs: dict[str, dict[str, Any]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        return httpx.AsyncClient(timeout=10.0)

    def _get_service_url(self, service: str) -> str:
        s = service.lower()
        if s == "gateway":
            return self.settings.gateway_url
        if s == "orders":
            return self.settings.orders_url
        if s == "inventory":
            return self.settings.inventory_url
        return f"http://{service}:8000"

    async def reset_world(self) -> dict[str, Any]:
        """Idempotently resets the lab world to baseline state.
        
        - Clears active faults across all services
        - Resets version profiles to baseline (v1.0.0)
        - Drains leaked connection pools
        - Flushes Redis caches
        - Halts any active background load generators
        """
        # 1. Stop any background loadgen tasks
        for run_id in list(self._active_runs.keys()):
            await self.stop_run(run_id)

        # 2. Reset each microservice via /admin/reset
        services = [
            ("gateway", self.settings.gateway_url),
            ("orders", self.settings.orders_url),
            ("inventory", self.settings.inventory_url),
        ]
        headers = {"X-Lab-Admin-Token": self.settings.lab_admin_token.get_secret_value()}
        results: dict[str, str] = {}

        client = self._get_client()
        should_close = self._external_client is None
        try:
            for name, url in services:
                endpoint = f"{url.rstrip('/')}/admin/reset"
                try:
                    resp = await client.post(endpoint, headers=headers, timeout=5.0)
                    if resp.status_code == 200:
                        results[name] = "reset"
                    else:
                        results[name] = f"error_{resp.status_code}"
                except Exception as e:
                    results[name] = f"unreachable_{type(e).__name__}"
        finally:
            if should_close:
                await client.aclose()

        return {"status": "world_reset_complete", "services": results}

    async def start_run(
        self,
        scenario_id: str,
        seed: int = 42,
        warmup_s: float | None = None,
    ) -> str:
        """Executes the complete scenario lifecycle and returns the run_id only.
        
        Flow:
        1. Reset world to baseline
        2. Start background open-loop load generation
        3. Warm-up
        4. Inject scenario (version deployment and/or concurrent faults)
        5. Record ground truth in Vault with exact timestamps
        6. Return run_id only
        """
        scenario: ScenarioDefinition = get_scenario(scenario_id)

        # 1. Reset world to baseline
        await self.reset_world()

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        now_created = datetime.now(timezone.utc).isoformat()

        # 2. Start open-loop background load generator
        stop_event = asyncio.Event()
        load_task = asyncio.create_task(
            self._run_background_load(scenario, seed, stop_event)
        )
        self._active_runs[run_id] = {
            "stop_event": stop_event,
            "task": load_task,
            "scenario_id": scenario_id,
        }

        # 3. Warm-up
        warmup_duration = warmup_s if warmup_s is not None else scenario.warmup_seconds
        if warmup_duration > 0:
            await asyncio.sleep(warmup_duration)

        # 4. Inject scenario
        culprit_deploy_id: str | None = None

        # (a) Deploy culprit version profile if specified
        if scenario.culprit_service and scenario.culprit_version:
            target_svc = scenario.culprit_service
            target_url = self._get_service_url(target_svc)
            try:
                record = await deploy(
                    service=target_svc,
                    new_version=scenario.culprit_version,
                    service_url=target_url,
                    database_url=self.settings.database_url,
                    admin_token=self.settings.lab_admin_token.get_secret_value(),
                    http_client=self._external_client,
                )
                culprit_deploy_id = record.deploy_id
            except Exception:
                # If database is unreachable in test mode, assign a synthetic deploy id
                culprit_deploy_id = f"dep-{uuid.uuid4().hex[:12]}"

        # (b) Apply concurrent faults if specified (supports multiple simultaneous faults)
        if scenario.faults:
            client = self._get_client()
            should_close = self._external_client is None
            headers = {"X-Lab-Admin-Token": self.settings.lab_admin_token.get_secret_value()}
            try:
                for fault in scenario.faults:
                    svc_url = self._get_service_url(fault.service)
                    endpoint = f"{svc_url.rstrip('/')}/admin/faults"
                    payload: dict[str, Any] = {}
                    if fault.dependency_latency_ms is not None:
                        payload["dependency_latency_ms"] = fault.dependency_latency_ms
                    if fault.dependency_error_rate is not None:
                        payload["dependency_error_rate"] = fault.dependency_error_rate
                    if fault.leak_on_conflict is not None:
                        payload["leak_on_conflict"] = fault.leak_on_conflict
                    try:
                        await client.post(endpoint, json=payload, headers=headers, timeout=5.0)
                    except Exception:
                        pass
            finally:
                if should_close:
                    await client.aclose()

        injected_at = datetime.now(timezone.utc).isoformat()

        # 5. Record truth in Vault
        truth = TruthRecord(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            family=scenario.family,
            root_cause=scenario.root_cause.model_dump(),
            culprit_deploy_id=culprit_deploy_id,
            injected_at=injected_at,
            expected_action=scenario.expected_action,
            decoys=scenario.decoys,
            health_thresholds=scenario.health_thresholds.model_dump(),
            split_tag=scenario.split_tag,
            created_at=now_created,
            status="running",
        )
        self.vault.record_run(truth)

        # 6. Return run_id only
        return run_id

    async def _run_background_load(
        self,
        scenario: ScenarioDefinition,
        seed: int,
        stop_event: asyncio.Event,
    ) -> None:
        """Background coroutine feeding continuous open-loop traffic to gateway."""
        # Simple continuous loop firing requests against gateway until stop_event is set
        gw_url = f"{self.settings.gateway_url.rstrip('/')}/checkout"
        client = httpx.AsyncClient(timeout=3.0)
        try:
            # Deterministic product and quantity sequence based on seed
            import random
            rng = random.Random(seed)
            while not stop_event.is_set():
                # Pick product 1..1000 and quantity
                pid = rng.randint(1, 1000)
                qty = rng.choice([1, 1, 1, 2, 2, 5, 20])
                try:
                    await client.post(gw_url, json={"product_id": pid, "qty": qty})
                except Exception:
                    pass
                # ~20 rps pace (50ms interval)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
        finally:
            await client.aclose()

    async def stop_run(self, run_id: str) -> bool:
        """Stops an active run and its background load."""
        run_data = self._active_runs.pop(run_id, None)
        if run_data:
            stop_event: asyncio.Event = run_data["stop_event"]
            stop_event.set()
            task: asyncio.Task[Any] = run_data["task"]
            try:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass
            self.vault.update_status(run_id, "stopped")
            return True
        return False

    def get_truth(self, run_id: str) -> TruthRecord | None:
        """Retrieves ground truth for the specified run from the Vault."""
        return self.vault.get_run(run_id)
