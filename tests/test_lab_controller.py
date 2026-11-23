from __future__ import annotations

import os
import re
from collections.abc import Generator
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import errors
from pydantic import SecretStr

from apps.api.settings import get_settings
from lab.common.faults import FaultState
from lab.common.metrics import LabMetrics
from lab.controller_lib.deploy import deploy, get_deploy_history, rollback, seed_deploy_history
from lab.controller_lib.metadata import (
    METADATA_REGISTRY,
    get_historical_seed_records,
    get_version_metadata,
)
from lab.seed.seed import seed_database
from lab.services.orders.client import OrdersStore, create_default_version_registry
from lab.services.orders.main import create_orders_app
from lab.services.orders.settings import OrdersSettings

FORBIDDEN_WORDS_REGEX = re.compile(
    r"\b(bug|bugs|buggy|leak|leaks|leaking|leaked|regression|regressions|flaw|flaws|defect|defects|broken|faulty)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def controller_test_db_url() -> Generator[str, None, None]:
    """Provisions a clean, dedicated test database with schema and roles initialized."""
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    test_db_name = "lab_controller_test_db"

    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            cur.execute(f"CREATE DATABASE {test_db_name};")

    test_db_url = urlunsplit(parsed._replace(path=f"/{test_db_name}"))
    seed_database(test_db_url, seed=42, init_roles=True, seed_deploys=False)

    yield test_db_url

    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{test_db_name}' AND pid <> pg_backend_pid();
                """
            )
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")


def test_metadata_and_diffs_contain_no_forbidden_words() -> None:
    """Verifies that all version metadata, summaries, and diffs contain no forbidden words."""
    # 1. Check metadata registry entries
    for (service, old_v, new_v), meta in METADATA_REGISTRY.items():
        for field_name, value in [
            ("summary", meta.summary),
            ("diff_text", meta.diff_text),
            ("author", meta.author),
        ]:
            matches = FORBIDDEN_WORDS_REGEX.findall(value)
            assert not matches, (
                f"Forbidden word {matches} in {service} {old_v}->{new_v} {field_name}: '{value}'"
            )

    # 2. Check historical seed log entries
    for record in get_historical_seed_records():
        for field_name in ["summary", "diff_text", "author"]:
            val = record[field_name]
            matches = FORBIDDEN_WORDS_REGEX.findall(val)
            assert not matches, (
                f"Forbidden word {matches} in seed record {record['deploy_id']} {field_name}"
            )

    # 3. Check fallback generator
    for s in ["orders", "inventory", "gateway"]:
        fallback = get_version_metadata(s, "v1.2.0", "v1.0.0")
        assert not FORBIDDEN_WORDS_REGEX.findall(fallback.summary)
        assert not FORBIDDEN_WORDS_REGEX.findall(fallback.diff_text)


@pytest.mark.asyncio
async def test_deploy_writes_exactly_one_row(
    controller_test_db_url: str,
) -> None:
    """Verifies that deploy() calls admin API and writes exactly one row to lab.deploys."""
    parsed = urlsplit(controller_test_db_url)
    orders_db = urlunsplit(
        parsed._replace(netloc=f"orders_rw:orders-rw-local-pw@{parsed.hostname}:{parsed.port}")
    )
    admin_db = urlunsplit(
        parsed._replace(
            netloc=f"controller_admin:controller-admin-local-pw@{parsed.hostname}:{parsed.port}"
        )
    )

    orders_settings = OrdersSettings(
        database_url=orders_db,
        lab_admin_token=SecretStr("super-secret-admin-token"),
    )
    orders_store = OrdersStore(
        settings=orders_settings,
        fault_state=FaultState(),
        metrics=LabMetrics(),
        version_registry=create_default_version_registry(),
    )
    orders_app = create_orders_app(settings=orders_settings, store=orders_store)

    transport = ASGITransport(app=orders_app)
    async with AsyncClient(transport=transport, base_url="http://orders") as client:
        # Check initial deploy count
        initial_history = get_deploy_history(service="orders", database_url=admin_db)
        initial_count = len(initial_history)

        # Deploy v1.1.0
        record = await deploy(
            service="orders",
            new_version="v1.1.0",
            service_url="http://orders",
            admin_token="super-secret-admin-token",
            database_url=admin_db,
            http_client=client,
        )

        assert record.service == "orders"
        assert record.old_version == "v1.0.0"
        assert record.new_version == "v1.1.0"
        assert record.deploy_id.startswith("dep-")
        assert "receipt formatting" in record.summary.lower()
        assert len(record.changed_files) > 0
        assert "receipt_version" in record.diff_text

        # Verify exactly one new row was persisted
        post_history = get_deploy_history(service="orders", database_url=admin_db)
        assert len(post_history) == initial_count + 1
        assert post_history[0].deploy_id == record.deploy_id


@pytest.mark.asyncio
async def test_rollback_restores_earlier_behaviour(
    controller_test_db_url: str,
) -> None:
    """Verifies that rollback(service) restores earlier behaviour (connection pool release)."""
    parsed = urlsplit(controller_test_db_url)
    orders_db = urlunsplit(
        parsed._replace(netloc=f"orders_rw:orders-rw-local-pw@{parsed.hostname}:{parsed.port}")
    )
    admin_db = urlunsplit(
        parsed._replace(
            netloc=f"controller_admin:controller-admin-local-pw@{parsed.hostname}:{parsed.port}"
        )
    )

    orders_settings = OrdersSettings(
        database_url=orders_db,
        lab_admin_token=SecretStr("super-secret-admin-token"),
    )
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
        # 1. Deploy v1.3.0 (retains connections on 409 conflict)
        rec1 = await deploy(
            service="orders",
            new_version="v1.3.0",
            service_url="http://orders",
            admin_token="super-secret-admin-token",
            database_url=admin_db,
            http_client=client,
        )
        assert rec1.new_version == "v1.3.0"
        assert orders_store.version_registry.active_version == "v1.3.0"

        # Mock an inventory 409 response by creating an order with excessive quantity
        # In v1.3.0, triggering 409 retains connection in the pool
        def mock_stock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"product_id": 1, "sku": "SKU-001", "qty": 0})

        orders_store.inventory_client = AsyncClient(
            transport=httpx.MockTransport(mock_stock_handler), base_url="http://inventory"
        )

        res_409 = await client.post("/orders", json={"product_id": 1, "qty": 10})
        assert res_409.status_code == 409
        pool_in_use_under_v130 = orders_store.pool_in_use
        assert pool_in_use_under_v130 == 1, "v1.3.0 should leak 1 connection on 409 conflict"

        # 2. Execute rollback("orders")
        rec_rollback = await rollback(
            service="orders",
            service_url="http://orders",
            admin_token="super-secret-admin-token",
            database_url=admin_db,
            http_client=client,
        )

        # Assert rollback wrote a new row restoring v1.0.0
        assert rec_rollback.service == "orders"
        assert rec_rollback.old_version == "v1.3.0"
        assert rec_rollback.new_version == "v1.0.0"
        assert orders_store.version_registry.active_version == "v1.0.0"

        # Pool was reset on deploy / rollback
        assert orders_store.pool_in_use == 0

        # Now test 409 on restored v1.0.0: connection must be released
        res_409_v100 = await client.post("/orders", json={"product_id": 1, "qty": 10})
        assert res_409_v100.status_code == 409
        assert orders_store.pool_in_use == 0, "v1.0.0 must cleanly release connection on 409"

    await orders_store.close()


def test_evidence_ro_can_select_only(
    controller_test_db_url: str,
) -> None:
    """Verifies that evidence_ro can SELECT from lab.deploys, but cannot INSERT/UPDATE/DELETE."""
    parsed = urlsplit(controller_test_db_url)
    evid_db = urlunsplit(
        parsed._replace(netloc=f"evidence_ro:evidence-ro-local-pw@{parsed.hostname}:{parsed.port}")
    )
    admin_db = urlunsplit(
        parsed._replace(
            netloc=f"controller_admin:controller-admin-local-pw@{parsed.hostname}:{parsed.port}"
        )
    )

    # 1. Seed historical records as admin
    seed_deploy_history(admin_db)

    # 2. Connect as evidence_ro
    with psycopg.connect(evid_db) as conn:
        with conn.cursor() as cur:
            # SELECT from lab.deploys allowed
            cur.execute("SELECT deploy_id, service, new_version, diff_text FROM lab.deploys;")
            rows = cur.fetchall()
            assert len(rows) >= 3

            # SELECT from public.deploys allowed
            cur.execute("SELECT COUNT(*) FROM deploys;")
            row_cnt = cur.fetchone()
            assert row_cnt is not None and row_cnt[0] >= 3

            # Forbidden INSERT into lab.deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(
                    """
                    INSERT INTO lab.deploys (
                        deploy_id, service, old_version, new_version, author, summary, diff_text
                    )
                    VALUES (
                        'dep-rogue', 'orders', 'v1.0.0', 'v2.0.0', 'attacker', 'summary', 'diff'
                    );
                    """
                )
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden UPDATE on lab.deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE lab.deploys SET summary = 'tampered' "
                    "WHERE deploy_id = 'dep-hist-001';"
                )
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden DELETE on lab.deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("DELETE FROM lab.deploys;")
        conn.rollback()
