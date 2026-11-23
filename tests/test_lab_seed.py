import os
from collections.abc import Generator
from urllib.parse import urlparse, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import errors

from apps.api.settings import get_settings
from lab.seed.seed import seed_database


@pytest.fixture(scope="module")
def lab_test_db_url() -> Generator[str, None, None]:
    """Provisions a clean, dedicated PostgreSQL database for lab seed and role testing."""
    admin_url = os.getenv("DATABASE_URL")
    if not admin_url:
        admin_url = get_settings().database_url.get_secret_value()
    clean_admin_url = admin_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(clean_admin_url)
    test_db_name = "lab_seed_test_db"

    # Connect to the admin maintenance DB with autocommit to provision test database
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")
            cur.execute(f"CREATE DATABASE {test_db_name};")

    test_db_url = urlunsplit(parsed._replace(path=f"/{test_db_name}"))

    yield test_db_url

    # Teardown test database
    with psycopg.connect(clean_admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Terminate active backend connections first
            cur.execute(
                f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{test_db_name}' AND pid <> pg_backend_pid();
                """
            )
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name};")


def test_seed_determinism_and_checksums(lab_test_db_url: str) -> None:
    """Verifies that two runs with the same seed produce identical row counts and checksums."""
    res1 = seed_database(lab_test_db_url, seed=42, init_roles=True)

    assert res1["row_counts"]["products"] == 1000
    assert res1["row_counts"]["inventory"] == 1000
    assert res1["row_counts"]["orders"] == 5000

    checksum_1 = res1["checksums"]["combined"]
    assert len(checksum_1) == 64

    # Run second time with identical seed (tests idempotency)
    res2 = seed_database(lab_test_db_url, seed=42, init_roles=True)

    assert res2["row_counts"]["products"] == 1000
    assert res2["row_counts"]["inventory"] == 1000
    assert res2["row_counts"]["orders"] == 5000

    checksum_2 = res2["checksums"]["combined"]

    assert checksum_1 == checksum_2
    assert res1["checksums"]["products"] == res2["checksums"]["products"]
    assert res1["checksums"]["inventory"] == res2["checksums"]["inventory"]
    assert res1["checksums"]["orders"] == res2["checksums"]["orders"]


def test_different_seed_produces_different_checksum(lab_test_db_url: str) -> None:
    """Verifies that differing RNG seeds produce distinct data checksums."""
    res_42 = seed_database(lab_test_db_url, seed=42, init_roles=False)
    res_99 = seed_database(lab_test_db_url, seed=99, init_roles=False)

    assert res_42["checksums"]["combined"] != res_99["checksums"]["combined"]
    assert res_42["checksums"]["products"] != res_99["checksums"]["products"]
    assert res_42["checksums"]["inventory"] != res_99["checksums"]["inventory"]
    assert res_42["checksums"]["orders"] != res_99["checksums"]["orders"]


def test_role_least_privilege_enforcement(lab_test_db_url: str) -> None:
    """Verifies that database roles cannot exceed their strictly granted privileges."""
    # Ensure roles and seed data are initialized
    seed_database(lab_test_db_url, seed=42, init_roles=True)

    parsed = urlparse(lab_test_db_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    db_name = parsed.path.lstrip("/")

    inv_pw = os.getenv("LAB_INVENTORY_RO_PASSWORD", "inv-ro-local-pw")
    orders_pw = os.getenv("LAB_ORDERS_RW_PASSWORD", "orders-rw-local-pw")
    evid_pw = os.getenv("LAB_EVIDENCE_RO_PASSWORD", "evidence-ro-local-pw")
    admin_pw = os.getenv("LAB_CONTROLLER_ADMIN_PASSWORD", "controller-admin-local-pw")

    # 1. Test inventory_ro: read products/inventory, NO write, NO orders/deploys access
    inv_url = f"postgresql://inventory_ro:{inv_pw}@{host}:{port}/{db_name}"
    with psycopg.connect(inv_url) as conn:
        with conn.cursor() as cur:
            # Read allowed
            cur.execute("SELECT COUNT(*) FROM products;")
            row_prod = cur.fetchone()
            assert row_prod is not None and row_prod[0] == 1000
            cur.execute("SELECT COUNT(*) FROM inventory;")
            row_inv = cur.fetchone()
            assert row_inv is not None and row_inv[0] == 1000

            # Forbidden write on products
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("INSERT INTO products (sku, name, price_cents) VALUES ('X', 'X', 10);")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden write on inventory
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("UPDATE inventory SET qty = 999 WHERE product_id = 1;")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden read on orders
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM orders LIMIT 1;")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden read on deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM deploys LIMIT 1;")
        conn.rollback()

    # 2. Test orders_rw: read products/inventory, read/write orders, NO write on inventory
    orders_url = f"postgresql://orders_rw:{orders_pw}@{host}:{port}/{db_name}"
    with psycopg.connect(orders_url) as conn:
        with conn.cursor() as cur:
            # Read allowed
            cur.execute("SELECT COUNT(*) FROM inventory;")
            row_inv = cur.fetchone()
            assert row_inv is not None and row_inv[0] == 1000

            # Read & Write allowed on orders
            cur.execute(
                """
                INSERT INTO orders (product_id, qty, status)
                VALUES (1, 2, 'PENDING') RETURNING id;
                """
            )
            row_id = cur.fetchone()
            assert row_id is not None and row_id[0] > 0
            new_id = row_id[0]
            cur.execute("UPDATE orders SET status = 'CONFIRMED' WHERE id = %s;", (new_id,))

            # Forbidden write on inventory
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("UPDATE inventory SET qty = 100 WHERE product_id = 1;")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden write on products
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("INSERT INTO products (sku, name, price_cents) VALUES ('Y', 'Y', 20);")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden access to deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM deploys LIMIT 1;")
        conn.rollback()

    # 3. Test evidence_ro: read-only on deploys ONLY
    evid_url = f"postgresql://evidence_ro:{evid_pw}@{host}:{port}/{db_name}"
    with psycopg.connect(evid_url) as conn:
        with conn.cursor() as cur:
            # Read deploys allowed
            cur.execute("SELECT COUNT(*) FROM deploys;")
            row_dep = cur.fetchone()
            assert row_dep is not None and row_dep[0] == 0

            # Forbidden write on deploys
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO deploys (service, version, commit_sha, deployed_by, status) "
                    "VALUES ('orders', 'v1.0.0', 'abc1234', 'ci', 'SUCCESS');"
                )
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden read on products
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM products LIMIT 1;")
        conn.rollback()

        with conn.cursor() as cur:
            # Forbidden read on orders
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM orders LIMIT 1;")
        conn.rollback()

    # 4. Test controller_admin: full access on all tables
    admin_conn_url = f"postgresql://controller_admin:{admin_pw}@{host}:{port}/{db_name}"
    with psycopg.connect(admin_conn_url) as conn:
        with conn.cursor() as cur:
            # Read allowed everywhere
            cur.execute("SELECT COUNT(*) FROM products;")
            cur.execute("SELECT COUNT(*) FROM inventory;")
            cur.execute("SELECT COUNT(*) FROM orders;")

            # Write allowed on deploys
            cur.execute(
                "INSERT INTO deploys (service, version, commit_sha, deployed_by, status) "
                "VALUES ('inventory', 'v1.0.0', 'deadbeef', 'admin', 'ACTIVE');"
            )
        conn.commit()
