import argparse
import asyncio
import os
import statistics
import time

import httpx

from lab.common.faults import FaultState
from lab.common.metrics import LabMetrics
from lab.services.inventory.client import InventoryStore, create_default_version_registry
from lab.services.inventory.main import create_inventory_app
from lab.services.inventory.settings import InventorySettings


async def run_benchmark(
    app_url: str | None = None,
    total_requests: int = 100,
    target_rps: float = 20.0,
) -> None:
    """Executes a 20 RPS benchmark comparing v1.0.0 (cache-aside) vs v1.2.0 (cache bypass + N+1)."""
    admin_headers = {"X-Lab-Admin-Token": "lab-admin-dev-token"}
    product_ids = [((i % 50) + 1) for i in range(total_requests)]
    interval = 1.0 / target_rps

    # If app_url is not provided, run in-process using AsyncClient and test store
    if app_url:
        client = httpx.AsyncClient(base_url=app_url)
    else:
        db_url = os.getenv(
            "LAB_DATABASE_URL",
            "postgresql://inventory_ro:inv-ro-local-pw@127.0.0.1:5432/lab",
        )
        redis_url = os.getenv("LAB_REDIS_URL", "redis://127.0.0.1:6379/0")

        settings = InventorySettings(
            service_name="inventory",
            version="v1.0.0",
            database_url=db_url,
            redis_url=redis_url,
            db_pool_size=10,
        )
        metrics = LabMetrics()
        fault_state = FaultState()
        registry = create_default_version_registry()
        store = InventoryStore(
            settings=settings,
            fault_state=fault_state,
            metrics=metrics,
            version_registry=registry,
        )
        await store.open()
        app = create_inventory_app(settings=settings, store=store, metrics=metrics)
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://benchmark")

    try:
        # Step 1: Benchmark v1.0.0
        # Ensure v1.0.0 is active
        await client.post("/admin/reset", headers=admin_headers)

        # Pre-warm cache for the 50 products
        for pid in set(product_ids):
            await client.get(f"/stock/{pid}")

        latencies_v100: list[float] = []
        for pid in product_ids:
            t0 = time.perf_counter()
            res = await client.get(f"/stock/{pid}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert res.status_code == 200
            latencies_v100.append(elapsed_ms)
            await asyncio.sleep(interval)

        # Step 2: Switch to v1.2.0 (F2 Regression: Cache Bypass & Extra DB Query)
        deploy_res = await client.post(
            "/admin/deploy",
            json={"version": "v1.2.0"},
            headers=admin_headers,
        )
        assert deploy_res.status_code == 200

        latencies_v120: list[float] = []
        for pid in product_ids:
            t0 = time.perf_counter()
            res = await client.get(f"/stock/{pid}")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert res.status_code == 200
            latencies_v120.append(elapsed_ms)
            await asyncio.sleep(interval)

        # Calculate statistics
        latencies_v100.sort()
        latencies_v120.sort()

        p50_v100 = statistics.median(latencies_v100)
        p95_v100 = latencies_v100[int(len(latencies_v100) * 0.95)]
        p99_v100 = latencies_v100[int(len(latencies_v100) * 0.99)]
        avg_v100 = statistics.mean(latencies_v100)

        p50_v120 = statistics.median(latencies_v120)
        p95_v120 = latencies_v120[int(len(latencies_v120) * 0.95)]
        p99_v120 = latencies_v120[int(len(latencies_v120) * 0.99)]
        avg_v120 = statistics.mean(latencies_v120)

        print("\n==========================================================================")
        print("          OpsPilot Inventory Benchmark: v1.0.0 vs v1.2.0 @ 20 RPS         ")
        print("==========================================================================")
        print(f"Total Requests: {total_requests} | Target Rate: {target_rps} RPS")
        print("-" * 74)
        header = (
            f"{'Version':<10} | {'Status':<18} | {'Avg (ms)':<9} | "
            f"{'p50 (ms)':<9} | {'p95 (ms)':<9} | {'p99 (ms)':<9}"
        )
        print(header)
        print("-" * 74)
        row_v100 = (
            f"{'v1.0.0':<10} | {'Cache-Aside (Hit)':<18} | {avg_v100:<9.2f} | "
            f"{p50_v100:<9.2f} | {p95_v100:<9.2f} | {p99_v100:<9.2f}"
        )
        print(row_v100)
        row_v120 = (
            f"{'v1.2.0':<10} | {'Cache Bypass+Extra':<18} | {avg_v120:<9.2f} | "
            f"{p50_v120:<9.2f} | {p95_v120:<9.2f} | {p99_v120:<9.2f}"
        )
        print(row_v120)
        print("==========================================================================")
        factor = p95_v120 / p95_v100 if p95_v100 > 0 else 0
        print(f"Regression Signature: v1.2.0 p95 latency is {factor:.2f}x of v1.0.0\n")

        # Reset back to v1.0.0
        await client.post("/admin/reset", headers=admin_headers)

    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark inventory service under 20 RPS")
    parser.add_argument(
        "--url", type=str, default=None, help="Base URL of running inventory service"
    )
    parser.add_argument(
        "--requests", type=int, default=100, help="Total requests to send per version"
    )
    parser.add_argument("--rps", type=float, default=20.0, help="Target RPS")
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(
            app_url=args.url,
            total_requests=args.requests,
            target_rps=args.rps,
        )
    )


if __name__ == "__main__":
    main()
