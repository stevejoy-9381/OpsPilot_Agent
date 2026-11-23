"""Acceptance benchmark for orders service: 2,000 seeded requests on v1.0.0 and v1.3.0.

Demonstrates:
- 5% of seeded traffic hits the 409 (insufficient stock) path.
- v1.0.0: connection is always released in finally; db_pool_in_use stays flat (0/10),
  503 error rate is 0.0%.
- v1.3.0: connection is retained on 409; db_pool_in_use rises gradually to 10/10,
  causing pool exhaustion and high 503 error rates.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg

from apps.api.settings import get_settings
from lab.common.faults import FaultState
from lab.common.metrics import LabMetrics
from lab.seed.seed import seed_database
from lab.services.orders.client import OrdersStore, create_default_version_registry
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings


def generate_seeded_requests(count: int = 2000, seed: int = 42) -> list[tuple[int, int]]:
    """Generates deterministic order requests where ~5% hit the 409 stock limit."""
    rng = random.Random(seed)
    requests: list[tuple[int, int]] = []
    for _ in range(count):
        product_id = rng.randint(1, 1000)
        # Seeded catalog has item stocks up to 500; ~5% request bulk purchases >500
        if rng.random() < 0.05:
            qty = rng.randint(501, 700)
        else:
            qty = rng.randint(1, 5)
        requests.append((product_id, qty))
    return requests


def make_mock_inventory_handler(
    catalog: dict[int, int],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/stock/"):
            try:
                pid = int(path.split("/")[-1])
            except ValueError:
                return httpx.Response(400, json={"detail": "Bad ID"})
            stock = catalog.get(pid, 50)
            return httpx.Response(200, json={"product_id": pid, "qty": stock})
        return httpx.Response(404, json={"detail": "Not found"})
    return handler


async def run_acceptance_benchmark() -> None:
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    bench_db = "lab_orders_acceptance_db"

    bench_db_url = urlunsplit(parsed._replace(path=f"/{bench_db}"))
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {bench_db};")
            cur.execute(f"CREATE DATABASE {bench_db};")

    seed_database(bench_db_url, seed=42, init_roles=True)

    # Load initial inventory stock levels from seeded database
    catalog: dict[int, int] = {}
    with psycopg.connect(bench_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT product_id, qty FROM inventory;")
            for pid, stock in cur.fetchall():
                catalog[pid] = stock

    user_pass = "orders_rw:orders-rw-local-pw"
    db_url_with_role = urlunsplit(
        parsed._replace(netloc=f"{user_pass}@{parsed.hostname}:{parsed.port}", path=f"/{bench_db}")
    )

    settings = OrdersSettings(
        database_url=db_url_with_role,
        inventory_url="http://mock-inventory",
        db_pool_size=10,
        db_pool_timeout_s=0.05,  # Fast wait timeout for high-throughput benchmark
        inventory_timeout_s=0.5,
    )
    metrics = LabMetrics()
    fault_state = FaultState()
    registry = create_default_version_registry()

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(make_mock_inventory_handler(catalog)),
        base_url=settings.inventory_url,
    )
    store = OrdersStore(
        settings=settings,
        fault_state=fault_state,
        metrics=metrics,
        version_registry=registry,
        inventory_client=mock_client,
    )
    app = create_orders_app(settings=settings, store=store, metrics=metrics)
    await store.open()

    traffic = generate_seeded_requests(count=2000, seed=42)

    print("=======================================================================")
    print("      OpsPilot Lab Orders Acceptance Benchmark: 2,000 Requests        ")
    print("=======================================================================")
    print(f"Total Requests: {len(traffic)}")
    print(f"Database Pool Capacity: {settings.db_pool_size}")
    print(f"Pool Wait Timeout: {settings.db_pool_timeout_s * 1000:.0f}ms\n")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Lab-Admin-Token": settings.lab_admin_token.get_secret_value()}

        # -------------------------------------------------------------
        # 1. Benchmark v1.0.0 (Clean Release in finally block)
        # -------------------------------------------------------------
        print("[Run 1/2] Running 2,000 seeded requests on v1.0.0 (Baseline)...")
        await client.post("/admin/deploy", headers=headers, json={"version": "v1.0.0"})

        v100_counts = {201: 0, 409: 0, 503: 0, "other": 0}
        v100_pool_trend: list[tuple[int, int]] = []
        t0_v100 = time.perf_counter()

        for idx, (pid, qty) in enumerate(traffic, start=1):
            resp = await client.post("/orders", json={"product_id": pid, "qty": qty})
            status_code = resp.status_code
            if status_code in v100_counts:
                v100_counts[status_code] += 1
            else:
                v100_counts["other"] += 1

            if idx % 200 == 0 or idx == 1:
                in_use = int(store.metrics.db_pool_in_use.labels(service="orders")._value.get())
                v100_pool_trend.append((idx, in_use))

        dur_v100 = time.perf_counter() - t0_v100
        print(f"  v1.0.0 completed in {dur_v100:.2f}s ({len(traffic)/dur_v100:.1f} RPS)")
        print(
            f"  Results: 201: {v100_counts[201]} | "
            f"409: {v100_counts[409]} | 503: {v100_counts[503]}\n"
        )

        # -------------------------------------------------------------
        # 2. Benchmark v1.3.0 (F1 Connection Leak on 409 Conflict Path)
        # -------------------------------------------------------------
        print("[Run 2/2] Running 2,000 seeded requests on v1.3.0 (Regression)...")
        await client.post("/admin/deploy", headers=headers, json={"version": "v1.3.0"})

        v130_counts = {201: 0, 409: 0, 503: 0, "other": 0}
        v130_pool_trend: list[tuple[int, int]] = []
        t0_v130 = time.perf_counter()

        for idx, (pid, qty) in enumerate(traffic, start=1):
            resp = await client.post("/orders", json={"product_id": pid, "qty": qty})
            status_code = resp.status_code
            if status_code in v130_counts:
                v130_counts[status_code] += 1
            else:
                v130_counts["other"] += 1

            if idx % 200 == 0 or idx == 1:
                in_use = int(store.metrics.db_pool_in_use.labels(service="orders")._value.get())
                v130_pool_trend.append((idx, in_use))

        dur_v130 = time.perf_counter() - t0_v130
        print(f"  v1.3.0 completed in {dur_v130:.2f}s ({len(traffic)/dur_v130:.1f} RPS)")
        print(
            f"  Results: 201: {v130_counts[201]} | "
            f"409: {v130_counts[409]} | 503: {v130_counts[503]}\n"
        )

        # -------------------------------------------------------------
        # 3. Print Acceptance Comparison Tables
        # -------------------------------------------------------------
        print("-----------------------------------------------------------------------")
        print("                db_pool_in_use Progression Trend                      ")
        print("-----------------------------------------------------------------------")
        print(f"{'Request #':<12} | {'v1.0.0 db_pool_in_use':<22} | {'v1.3.0 db_pool_in_use':<22}")
        print("-" * 65)
        for (req_num, p_v100), (_, p_v130) in zip(v100_pool_trend, v130_pool_trend, strict=True):
            print(f"{req_num:<12} | {f'{p_v100}/10':<22} | {f'{p_v130}/10':<22}")

        print("\n-----------------------------------------------------------------------")
        print("                    Final Error Rate Comparison                       ")
        print("-----------------------------------------------------------------------")
        v100_err_rate = (v100_counts[503] / len(traffic)) * 100
        v130_err_rate = (v130_counts[503] / len(traffic)) * 100
        v100_409_rate = (v100_counts[409] / len(traffic)) * 100
        v130_409_rate = (v130_counts[409] / len(traffic)) * 100

        print("Metric                        | v1.0.0 (Baseline)    | v1.3.0 (Regression)")
        print("-" * 65)
        print(f"201 Success Count             | {v100_counts[201]:<20} | {v130_counts[201]:<20}")
        print(f"409 Insufficient Stock Rate   | {v100_409_rate:<19.1f}% | {v130_409_rate:<19.1f}%")
        print(f"503 Pool Timeout Rate         | {v100_err_rate:<19.1f}% | {v130_err_rate:<19.1f}%")
        peak_100 = max(p for _, p in v100_pool_trend)
        peak_130 = max(p for _, p in v130_pool_trend)
        print(f"Peak Pool Utilization         | {peak_100}/10                 | {peak_130}/10")
        print("=======================================================================\n")

    await store.close()
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{bench_db}' AND pid <> pg_backend_pid();
                """
            )
            cur.execute(f"DROP DATABASE IF EXISTS {bench_db};")


if __name__ == "__main__":
    asyncio.run(run_acceptance_benchmark())
