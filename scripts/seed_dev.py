"""Development and demonstration seed script for OpsPilot.

Provisions:
  - 2 tenants: "Acme Corp" (slug: acme-corp), "Globex Ltd" (slug: globex-ltd)
  - 1 viewer, 1 reviewer, 1 admin per tenant
  - Deterministic UUIDs and password hashes for test predictability.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from typing import Any

import psycopg


def deterministic_uuid(namespace: str, name: str) -> uuid.UUID:
    """Generate a deterministic UUIDv5 for repeatable seeding."""
    ns = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    return uuid.uuid5(ns, f"{namespace}:{name}")


def hash_password(password: str) -> str:
    """Deterministic hash for dev password."""
    return "dev_pbkdf2_" + hashlib.sha256(password.encode("utf-8")).hexdigest()[:32]


SEED_DATA: list[dict[str, Any]] = [
    {
        "tenant": {
            "id": deterministic_uuid("tenant", "acme-corp"),
            "slug": "acme-corp",
            "name": "Acme Corp",
        },
        "users": [
            {
                "id": deterministic_uuid("user", "acme-viewer"),
                "email": "viewer@acme.example.com",
                "role": "viewer",
                "password": "acme_viewer_pw",
            },
            {
                "id": deterministic_uuid("user", "acme-reviewer"),
                "email": "reviewer@acme.example.com",
                "role": "reviewer",
                "password": "acme_reviewer_pw",
            },
            {
                "id": deterministic_uuid("user", "acme-admin"),
                "email": "admin@acme.example.com",
                "role": "admin",
                "password": "acme_admin_pw",
            },
        ],
    },
    {
        "tenant": {
            "id": deterministic_uuid("tenant", "globex-ltd"),
            "slug": "globex-ltd",
            "name": "Globex Ltd",
        },
        "users": [
            {
                "id": deterministic_uuid("user", "globex-viewer"),
                "email": "viewer@globex.example.com",
                "role": "viewer",
                "password": "globex_viewer_pw",
            },
            {
                "id": deterministic_uuid("user", "globex-reviewer"),
                "email": "reviewer@globex.example.com",
                "role": "reviewer",
                "password": "globex_reviewer_pw",
            },
            {
                "id": deterministic_uuid("user", "globex-admin"),
                "email": "admin@globex.example.com",
                "role": "admin",
                "password": "globex_admin_pw",
            },
        ],
    },
]


def seed_database(db_url: str) -> None:
    """Inserts dev seed data idempotently into app.tenants and app.users."""
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for item in SEED_DATA:
                t = item["tenant"]
                cur.execute(
                    """
                    INSERT INTO app.tenants (id, slug, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name
                    RETURNING id;
                    """,
                    (t["id"], t["slug"], t["name"]),
                )
                tenant_id = cur.fetchone()[0]

                for u in item["users"]:
                    cur.execute(
                        """
                        INSERT INTO app.users (id, tenant_id, email, role, password_hash)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, email) DO UPDATE
                        SET role = EXCLUDED.role,
                            password_hash = EXCLUDED.password_hash;
                        """,
                        (u["id"], tenant_id, u["email"], u["role"], hash_password(u["password"])),
                    )
        conn.commit()
    print("Seed complete: 2 tenants, 6 users seeded successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed dev tenants and users into OpsPilot database."
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=os.environ.get(
            "DATABASE_URL", "postgresql://opspilot:change-me-local-only@127.0.0.1:5432/opspilot"
        ),
        help="PostgreSQL connection URL",
    )
    args = parser.parse_args()
    seed_database(args.db_url)


if __name__ == "__main__":
    main()
