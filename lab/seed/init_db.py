import os
from pathlib import Path

import psycopg
from psycopg import sql

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def apply_schema(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Applies the lab database schema from schema.sql."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()


def setup_roles(
    conn: psycopg.Connection[tuple[object, ...]],
    db_name: str = "lab",
) -> None:
    """Creates least-privilege database roles with passwords supplied from environment variables.
    
    Roles:
      - inventory_ro: read products/inventory
      - orders_rw: read inventory & products, read/write orders
      - evidence_ro: read-only on deploys only
      - controller_admin: full access
    """
    inventory_ro_pw = os.getenv("LAB_INVENTORY_RO_PASSWORD", "inv-ro-local-pw")
    orders_rw_pw = os.getenv("LAB_ORDERS_RW_PASSWORD", "orders-rw-local-pw")
    evidence_ro_pw = os.getenv("LAB_EVIDENCE_RO_PASSWORD", "evidence-ro-local-pw")
    controller_admin_pw = os.getenv("LAB_CONTROLLER_ADMIN_PASSWORD", "controller-admin-local-pw")

    with conn.cursor() as cur:
        # Create roles if they don't already exist
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'inventory_ro') THEN
                    CREATE ROLE inventory_ro WITH LOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orders_rw') THEN
                    CREATE ROLE orders_rw WITH LOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'evidence_ro') THEN
                    CREATE ROLE evidence_ro WITH LOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'controller_admin') THEN
                    CREATE ROLE controller_admin WITH LOGIN;
                END IF;
            END
            $$;
        """)

        # Set passwords
        cur.execute(
            sql.SQL("ALTER ROLE inventory_ro WITH LOGIN PASSWORD {}").format(
                sql.Literal(inventory_ro_pw)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE orders_rw WITH LOGIN PASSWORD {}").format(
                sql.Literal(orders_rw_pw)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE evidence_ro WITH LOGIN PASSWORD {}").format(
                sql.Literal(evidence_ro_pw)
            )
        )
        cur.execute(
            sql.SQL("ALTER ROLE controller_admin WITH LOGIN PASSWORD {}").format(
                sql.Literal(controller_admin_pw)
            )
        )

        # Revoke default public permissions to enforce strict least-privilege
        cur.execute("REVOKE ALL ON TABLE products, inventory, orders FROM PUBLIC;")
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;")

        # Grant schema usages
        cur.execute(
            "GRANT USAGE ON SCHEMA lab TO inventory_ro, orders_rw, evidence_ro, controller_admin;"
        )

        # Role 1: inventory_ro (read products, inventory)
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM inventory_ro;")
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM inventory_ro;")
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO inventory_ro;").format(sql.Identifier(db_name))
        )
        cur.execute("GRANT USAGE ON SCHEMA public TO inventory_ro;")
        cur.execute("GRANT SELECT ON products, inventory TO inventory_ro;")

        # Role 2: orders_rw (read inventory & products, read/write orders)
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM orders_rw;")
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM orders_rw;")
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO orders_rw;").format(sql.Identifier(db_name))
        )
        cur.execute("GRANT USAGE ON SCHEMA public TO orders_rw;")
        cur.execute("GRANT SELECT ON products, inventory TO orders_rw;")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON orders TO orders_rw;")
        cur.execute("GRANT USAGE, SELECT ON SEQUENCE orders_id_seq TO orders_rw;")

        # Role 3: evidence_ro (read-only on deploys only)
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM evidence_ro;")
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM evidence_ro;")
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO evidence_ro;").format(sql.Identifier(db_name))
        )
        cur.execute("GRANT USAGE ON SCHEMA public TO evidence_ro;")
        cur.execute("GRANT SELECT ON deploys TO evidence_ro;")
        cur.execute("GRANT SELECT ON lab.deploys TO evidence_ro;")

        # Role 4: controller_admin (full access)
        cur.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO controller_admin;").format(
                sql.Identifier(db_name)
            )
        )
        cur.execute("GRANT ALL PRIVILEGES ON SCHEMA public TO controller_admin;")
        cur.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO controller_admin;")
        cur.execute("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO controller_admin;")
        cur.execute("GRANT ALL PRIVILEGES ON SCHEMA lab TO controller_admin;")
        cur.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA lab TO controller_admin;")
        cur.execute("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA lab TO controller_admin;")

    conn.commit()
