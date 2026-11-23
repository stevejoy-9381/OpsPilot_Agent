"""Demonstrates a deploy and rollback cycle, displaying the resulting lab.deploys table."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
from httpx import ASGITransport, AsyncClient

from apps.api.settings import get_settings
from lab.common.faults import FaultState
from lab.common.metrics import LabMetrics
from lab.controller_lib.deploy import deploy, rollback
from lab.seed.seed import seed_database
from lab.services.orders.client import OrdersStore, create_default_version_registry
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings


async def main() -> None:
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    demo_db_name = "lab_deploy_demo_db"

    # 1. Setup temporary demo database
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {demo_db_name};")
            cur.execute(f"CREATE DATABASE {demo_db_name};")

    demo_db_url = urlunsplit(parsed._replace(path=f"/{demo_db_name}"))
    seed_database(demo_db_url, seed=42, init_roles=True, seed_deploys=True)

    orders_db = urlunsplit(
        parsed._replace(
            netloc=f"orders_rw:orders-rw-local-pw@{parsed.hostname}:{parsed.port}",
            path=f"/{demo_db_name}",
        )
    )
    admin_db = urlunsplit(
        parsed._replace(
            netloc=f"controller_admin:controller-admin-local-pw@{parsed.hostname}:{parsed.port}",
            path=f"/{demo_db_name}",
        )
    )
    evid_db = urlunsplit(
        parsed._replace(
            netloc=f"evidence_ro:evidence-ro-local-pw@{parsed.hostname}:{parsed.port}",
            path=f"/{demo_db_name}",
        )
    )

    # 2. Wire orders service
    orders_settings = OrdersSettings(database_url=orders_db)
    orders_store = OrdersStore(
        settings=orders_settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
    )
    orders_app = create_orders_app(settings=orders_settings, store=orders_store)
    await orders_store.open()

    transport = ASGITransport(app=orders_app)
    async with AsyncClient(transport=transport, base_url="http://orders") as client:
        print("\n" + "=" * 80)
        print("          STEP 1: DEPLOY orders TO v1.3.0          ")
        print("=" * 80)
        rec_deploy = await deploy(
            service="orders",
            new_version="v1.3.0",
            service_url="http://orders",
            admin_token="lab-admin-dev-token",
            database_url=admin_db,
            http_client=client,
        )
        print(f"Deploy ID:    {rec_deploy.deploy_id}")
        ver_transition = f"{rec_deploy.old_version}->{rec_deploy.new_version}"
        print(f"Service:      {rec_deploy.service} ({ver_transition})")
        print(f"Author:       {rec_deploy.author}")
        print(f"Summary:      {rec_deploy.summary}")
        print(f"Deployed At:  {rec_deploy.deployed_at.isoformat()}")

        print("\n" + "=" * 80)
        print("          STEP 2: ROLLBACK orders (RESTORES PREVIOUS VERSION)          ")
        print("=" * 80)
        rec_rollback = await rollback(
            service="orders",
            service_url="http://orders",
            admin_token="lab-admin-dev-token",
            database_url=admin_db,
            http_client=client,
        )
        print(f"Deploy ID:    {rec_rollback.deploy_id}")
        print(
            f"Service:      {rec_rollback.service} "
            f"({rec_rollback.old_version} -> {rec_rollback.new_version})"
        )
        print(f"Author:       {rec_rollback.author}")
        print(f"Summary:      {rec_rollback.summary}")
        print(f"Deployed At:  {rec_rollback.deployed_at.isoformat()}")

    await orders_store.close()

    # 3. Read deploy table as evidence_ro
    print("\n" + "=" * 80)
    print("       STEP 3: QUERY lab.deploys TABLE AS evidence_ro       ")
    print("=" * 80)
    with psycopg.connect(evid_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT deploy_id, service, old_version, new_version, deployed_at, author, summary
                FROM lab.deploys
                ORDER BY deployed_at ASC;
                """
            )
            rows = cur.fetchall()

            header = (
                f"{'DEPLOY ID':<16} | {'SERVICE':<10} | {'OLD':<6} | "
                f"{'NEW':<6} | {'AUTHOR':<22} | {'SUMMARY'}"
            )
            print(header)
            print("-" * len(header))
            for row in rows:
                d_id, svc, old_v, new_v, d_at, author, summary = row
                print(
                    f"{d_id:<16} | {svc:<10} | {old_v:<6} | {new_v:<6} | "
                    f"{author:<22} | {summary[:60]}"
                )

    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
