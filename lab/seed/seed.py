import argparse
import hashlib
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import psycopg

from lab.controller_lib.deploy import seed_deploy_history
from lab.seed.init_db import apply_schema, setup_roles

ProductRow = tuple[int, str, str, int]
InventoryRow = tuple[int, int]
OrderRow = tuple[int, int, int, str, datetime]


def generate_seed_data(
    seed: int,
) -> tuple[list[ProductRow], list[InventoryRow], list[OrderRow]]:
    """Deterministically generates 1,000 products, stock levels, and 5,000 historical orders."""
    rng = random.Random(seed)

    adjectives = [
        "Ultra", "Pro", "Eco", "Smart", "Compact",
        "Hyper", "Prime", "Core", "Nano", "Apex",
        "Quantum", "Dynamic", "Aero", "Pulse", "Vortex",
    ]
    categories = [
        "Widget", "Gadget", "Sensor", "Device", "Module",
        "Controller", "Node", "Hub", "Relay", "Drive",
        "Monitor", "Adapter", "Transceiver", "Valve", "Switch",
    ]

    # 1. Products (1,000 rows)
    products: list[tuple[int, str, str, int]] = []
    for i in range(1, 1001):
        sku = f"SKU-{i:05d}"
        name = f"{rng.choice(adjectives)} {rng.choice(categories)} {i}"
        price_cents = rng.randint(299, 99999)  # $2.99 to $999.99
        products.append((i, sku, name, price_cents))

    # 2. Inventory (1,000 rows)
    inventory: list[tuple[int, int]] = []
    for i in range(1, 1001):
        qty = rng.randint(0, 500)
        inventory.append((i, qty))

    # 3. Orders (5,000 rows)
    statuses = ["CONFIRMED", "SHIPPED", "DELIVERED", "PENDING", "CANCELLED"]
    status_weights = [0.55, 0.20, 0.15, 0.05, 0.05]
    base_time = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

    orders: list[tuple[int, int, int, str, datetime]] = []
    for j in range(1, 5001):
        product_id = rng.randint(1, 1000)
        qty = rng.randint(1, 8)
        status = rng.choices(statuses, weights=status_weights)[0]
        # Deterministically advancing timestamps
        offset_seconds = j * 180 + rng.randint(0, 120)
        created_at = base_time + timedelta(seconds=offset_seconds)
        orders.append((j, product_id, qty, status, created_at))

    return products, inventory, orders


def compute_table_checksums(conn: psycopg.Connection[tuple[object, ...]]) -> dict[str, str]:
    """Computes deterministic SHA-256 checksums across all seeded tables."""
    checksums: dict[str, str] = {}
    with conn.cursor() as cur:
        # 1. Products checksum
        cur.execute("SELECT id, sku, name, price_cents FROM products ORDER BY id")
        h_prod = hashlib.sha256()
        for row in cur.fetchall():
            h_prod.update(f"{row[0]}|{row[1]}|{row[2]}|{row[3]}\n".encode("utf-8"))
        checksums["products"] = h_prod.hexdigest()

        # 2. Inventory checksum
        cur.execute("SELECT product_id, qty FROM inventory ORDER BY product_id")
        h_inv = hashlib.sha256()
        for row in cur.fetchall():
            h_inv.update(f"{row[0]}|{row[1]}\n".encode("utf-8"))
        checksums["inventory"] = h_inv.hexdigest()

        # 3. Orders checksum
        cur.execute("SELECT id, product_id, qty, status, created_at FROM orders ORDER BY id")
        h_orders = hashlib.sha256()
        for row in cur.fetchall():
            ts_str = row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4])
            h_orders.update(f"{row[0]}|{row[1]}|{row[2]}|{row[3]}|{ts_str}\n".encode("utf-8"))
        checksums["orders"] = h_orders.hexdigest()

        # Combined composite checksum
        h_all = hashlib.sha256()
        h_all.update(f"{checksums['products']}:{checksums['inventory']}:{checksums['orders']}".encode("utf-8"))
        checksums["combined"] = h_all.hexdigest()

    return checksums


def seed_database(
    db_url: str,
    seed: int = 42,
    init_roles: bool = True,
    seed_deploys: bool = False,
) -> dict[str, Any]:
    """Executes deterministic, idempotent database seeding.
    
    Cache is intentionally NOT pre-warmed.
    """
    # Normalize postgresql+psycopg to standard postgresql for psycopg connection string
    conn_url = db_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlparse(conn_url)
    db_name = parsed.path.lstrip("/") or "lab"

    with psycopg.connect(conn_url) as conn:
        # Step 1: Ensure schema exists
        apply_schema(conn)

        # Step 2: Ensure least-privilege roles exist if requested
        if init_roles:
            setup_roles(conn, db_name=db_name)

        # Step 3: Generate deterministic seed data
        products, inventory, orders = generate_seed_data(seed)

        # Step 4: Idempotent replacement via TRUNCATE CASCADE
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE orders, inventory, products RESTART IDENTITY CASCADE;")

            cur.executemany(
                "INSERT INTO products (id, sku, name, price_cents) VALUES (%s, %s, %s, %s);",
                products,
            )
            cur.execute("SELECT setval('products_id_seq', (SELECT MAX(id) FROM products));")

            cur.executemany(
                "INSERT INTO inventory (product_id, qty) VALUES (%s, %s);",
                inventory,
            )

            cur.executemany(
                """
                INSERT INTO orders (id, product_id, qty, status, created_at)
                VALUES (%s, %s, %s, %s, %s);
                """,
                orders,
            )
            cur.execute("SELECT setval('orders_id_seq', (SELECT MAX(id) FROM orders));")

        conn.commit()

        if seed_deploys:
            seed_deploy_history(conn_url)

        # Step 5: Verify row counts and calculate checksums
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM products;")
            row = cur.fetchone()
            count_products = row[0] if row else 0

            cur.execute("SELECT COUNT(*) FROM inventory;")
            row = cur.fetchone()
            count_inventory = row[0] if row else 0

            cur.execute("SELECT COUNT(*) FROM orders;")
            row = cur.fetchone()
            count_orders = row[0] if row else 0

        checksums = compute_table_checksums(conn)

    return {
        "seed": seed,
        "row_counts": {
            "products": count_products,
            "inventory": count_inventory,
            "orders": count_orders,
        },
        "checksums": checksums,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic database seeder for OpsPilot lab")
    parser.add_argument("--seed", type=int, default=42, help="Seed integer for RNG (default: 42)")
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.getenv(
            "LAB_DATABASE_URL",
            os.getenv("DATABASE_URL", "postgresql://opspilot:opspilot_dev_password@127.0.0.1:5432/opspilot"),
        ),
        help="Database connection URL",
    )
    parser.add_argument("--no-roles", action="store_true", help="Skip roles initialization")
    parser.add_argument(
        "--seed-history", action="store_true", help="Seed historical deploy audit records"
    )

    args = parser.parse_args()

    result = seed_database(
        db_url=args.database_url,
        seed=args.seed,
        init_roles=not args.no_roles,
        seed_deploys=args.seed_history,
    )

    print(f"OpsPilot Lab Seed Complete (seed={result['seed']}):")
    print(f"  Products:   {result['row_counts']['products']} rows")
    print(f"  Inventory:  {result['row_counts']['inventory']} rows")
    print(f"  Orders:     {result['row_counts']['orders']} rows")
    print(f"  Combined Checksum: {result['checksums']['combined']}")
    print("  Table Checksums:")
    print(f"    products:  {result['checksums']['products']}")
    print(f"    inventory: {result['checksums']['inventory']}")
    print(f"    orders:    {result['checksums']['orders']}")


if __name__ == "__main__":
    main()
