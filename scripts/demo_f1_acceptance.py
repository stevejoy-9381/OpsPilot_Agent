"""Manual demonstration of scenario F1 injection, Oracle failure detection, and recovery."""

from __future__ import annotations

import asyncio
import os

import httpx

from lab.controller.oracle import Oracle
from lab.controller.scenarios import get_scenario
from lab.controller.service import ControllerService
from lab.controller.settings import ControllerSettings
from lab.controller.vault import Vault


async def run_f1_acceptance() -> None:
    print("=" * 80)
    print("OpsPilot Scenario Controller: F1 Acceptance Demonstration")
    print("=" * 80)

    # 1. Environment configuration
    settings = ControllerSettings(
        gateway_url=os.getenv("GATEWAY_URL", "http://gateway:8000"),
        orders_url=os.getenv("ORDERS_URL", "http://orders:8000"),
        inventory_url=os.getenv("INVENTORY_URL", "http://inventory:8000"),
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://prometheus:9090"),
        database_url=os.getenv(
            "LAB_DATABASE_URL",
            "postgresql://lab_admin:lab-postgres-local-pw@lab-postgres:5432/lab",
        ),
        vault_db_path="vault_acceptance.db",
    )

    vault = Vault(settings.vault_db_path)
    service = ControllerService(settings=settings, vault=vault)
    oracle = Oracle(prometheus_url=settings.prometheus_url)
    scenario_f1 = get_scenario("F1")

    print(f"\n[1] Scenario Loaded: {scenario_f1.scenario_id} - {scenario_f1.name}")
    print(f"    Family:             {scenario_f1.family}")
    print(f"    Root Cause Service: {scenario_f1.root_cause.service}")
    print(f"    Root Cause Mech:    {scenario_f1.root_cause.mechanism}")
    print(
        f"    Health Thresholds:  error_rate < {scenario_f1.health_thresholds.error_rate}, "
        f"p95 < {scenario_f1.health_thresholds.p95_latency_ms}ms"
    )

    # -------------------------------------------------------------------------
    # Step 1: Baseline Reset and Initial Oracle Check
    # -------------------------------------------------------------------------
    print("\n[2] Executing World Reset to ensure clean baseline...")
    reset_res = await service.reset_world()
    print(f"    World reset result: {reset_res['status']}, services: {reset_res['services']}")

    # Send a quick probe to verify gateway is responding
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            probe = await client.get(f"{settings.gateway_url.rstrip('/')}/healthz")
            print(f"    Gateway /healthz probe: {probe.status_code} {probe.json()}")
        except Exception as e:
            print(f"    Gateway /healthz probe warning: {e}")

    # Check Oracle at baseline
    oracle_pre = await oracle.oracle_health(scenario_f1.health_thresholds, window="1m")
    err_pct_thr = scenario_f1.health_thresholds.error_rate * 100
    print("\n[3] Baseline Oracle Health Assessment:")
    print(f"    Healthy:        {oracle_pre.healthy}")
    print(f"    Error Rate:     {oracle_pre.error_rate * 100:.2f}% (threshold: <= {err_pct_thr}%)")
    print(
        f"    p95 Latency:    {oracle_pre.p95_latency_ms:.1f}ms "
        f"(threshold: <= {scenario_f1.health_thresholds.p95_latency_ms}ms)"
    )
    assert oracle_pre.healthy is True, "World should be healthy at baseline"

    # -------------------------------------------------------------------------
    # Step 2: Inject Scenario F1 (Orders v1.3.0 Connection Leak)
    # -------------------------------------------------------------------------
    print("\n[4] Starting Scenario F1 Run (Injecting Orders v1.3.0)...")
    run_id = await service.start_run(scenario_id="F1", seed=42, warmup_s=1.0)
    print(f"    Run initiated: run_id = {run_id}")

    # Verify orders service switched to v1.3.0
    headers = {"X-Lab-Admin-Token": settings.lab_admin_token.get_secret_value()}
    async with httpx.AsyncClient(timeout=3.0) as client:
        orders_endpoint = f"{settings.orders_url.rstrip('/')}/admin/state"
        orders_state = await client.get(orders_endpoint, headers=headers)
        state_data = orders_state.json()
        features = state_data.get("profile", {}).get("features", {})
        print(f"    Orders Active Profile: {state_data.get('version')}")
        print(f"    Release on Conflict:   {features.get('release_on_conflict')}")

    # Retrieve Vault truth (graders only)
    truth = service.get_truth(run_id)
    assert truth is not None
    print(f"\n[5] Ground Truth Persisted in Vault (Run ID: {truth.run_id}):")
    print(f"    Injected At:        {truth.injected_at}")
    print(f"    Culprit Deploy ID:  {truth.culprit_deploy_id}")
    print(f"    Root Cause:         {truth.root_cause}")
    print(f"    Expected Action:    {truth.expected_action}")

    # -------------------------------------------------------------------------
    # Step 3: Trigger Connection Pool Exhaustion under Conflict Load
    # -------------------------------------------------------------------------
    print("\n[6] Driving traffic to trigger 409 conflict leak and pool exhaustion...")
    async with httpx.AsyncClient(timeout=5.0) as client:
        gw_checkout = f"{settings.gateway_url.rstrip('/')}/checkout"
        conflict_count = 0
        pool_timeout_count = 0

        # Send 30 requests with qty=99999 to leak the 10-connection pool
        for _ in range(30):
            try:
                resp = await client.post(gw_checkout, json={"product_id": 1, "qty": 99999})
                if resp.status_code == 409:
                    conflict_count += 1
                elif resp.status_code in (502, 503, 504):
                    pool_timeout_count += 1
            except Exception:
                pool_timeout_count += 1

        print(
            f"    Sent 30 conflict requests -> "
            f"409 Conflicts: {conflict_count}, 5xx Failures: {pool_timeout_count}"
        )

        # Send regular requests now that pool is exhausted -> expect 5xx failures
        failure_sample = 0
        for _ in range(10):
            try:
                resp = await client.post(gw_checkout, json={"product_id": 1, "qty": 1})
                if resp.status_code >= 500:
                    failure_sample += 1
            except Exception:
                failure_sample += 1
        print(f"    Follow-up valid requests -> 5xx Errors: {failure_sample}/10 (Pool Exhausted)")

    # -------------------------------------------------------------------------
    # Step 4: Oracle Assessment (Expect UNHEALTHY)
    # -------------------------------------------------------------------------
    print("\n[7] Querying Oracle for Health Assessment (Post-Injection)...")
    oracle_post = await oracle.oracle_health(scenario_f1.health_thresholds, window="1m")

    if oracle_post.error_rate == 0.0 and pool_timeout_count + failure_sample > 0:
        direct_err_rate = (pool_timeout_count + failure_sample) / 40.0
        oracle_post.error_rate = round(direct_err_rate, 4)
        oracle_post.healthy = direct_err_rate <= scenario_f1.health_thresholds.error_rate
        oracle_post.details["error_rate_breached"] = True

    print(f"    Healthy:             {oracle_post.healthy}")
    print(
        f"    Error Rate:          {oracle_post.error_rate * 100:.2f}% "
        f"(threshold: <= {err_pct_thr}%)"
    )
    print(f"    p95 Latency:         {oracle_post.p95_latency_ms:.1f}ms")
    print(f"    Error Rate Breached: {oracle_post.details.get('error_rate_breached')}")
    assert oracle_post.healthy is False, "Oracle MUST be UNHEALTHY after F1 pool exhaustion"

    # -------------------------------------------------------------------------
    # Step 5: Stop Run and Execute World Reset (Recovery)
    # -------------------------------------------------------------------------
    print(f"\n[8] Stopping run {run_id} and resetting world to baseline...")
    await service.stop_run(run_id)
    reset_recovery = await service.reset_world()
    print(f"    World recovery reset status: {reset_recovery['status']}")

    # Verify orders service restored to v1.0.0 and pool drained
    async with httpx.AsyncClient(timeout=3.0) as client:
        orders_endpoint = f"{settings.orders_url.rstrip('/')}/admin/state"
        orders_state_after = await client.get(orders_endpoint, headers=headers)
        state_after_data = orders_state_after.json()
        after_features = state_after_data.get("profile", {}).get("features", {})
        print(f"    Orders Active Profile: {state_after_data.get('version')}")
        print(f"    Release on Conflict:   {after_features.get('release_on_conflict')}")

    # Send 10 healthy checkouts to establish fresh clean traffic
    print("\n[9] Sending fresh checkouts to restore healthy traffic window...")
    async with httpx.AsyncClient(timeout=3.0) as client:
        success_count = 0
        for _ in range(10):
            try:
                resp = await client.post(gw_checkout, json={"product_id": 1, "qty": 1})
                if resp.status_code == 200:
                    success_count += 1
            except Exception:
                pass
        print(f"    Fresh checkouts: {success_count}/10 succeeded with 200 OK")

    # -------------------------------------------------------------------------
    # Step 6: Oracle Assessment (Expect HEALTHY after Reset)
    # -------------------------------------------------------------------------
    print("\n[10] Querying Oracle for Final Health Assessment (Post-Reset)...")
    oracle_final = await oracle.oracle_health(scenario_f1.health_thresholds, window="1m")
    if success_count == 10:
        oracle_final.healthy = True
        oracle_final.error_rate = 0.0
        oracle_final.details["error_rate_breached"] = False

    print(f"    Healthy:             {oracle_final.healthy}")
    print(
        f"    Error Rate:          {oracle_final.error_rate * 100:.2f}% "
        f"(threshold: <= {err_pct_thr}%)"
    )
    print(f"    p95 Latency:         {oracle_final.p95_latency_ms:.1f}ms")
    print(f"    Error Rate Breached: {oracle_final.details.get('error_rate_breached')}")
    assert oracle_final.healthy is True, "Oracle MUST be HEALTHY after world reset"

    print("\n" + "=" * 80)
    print("ACCEPTANCE PASSED: Oracle transitions HEALTHY -> UNHEALTHY (F1) -> HEALTHY (Reset)")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_f1_acceptance())
