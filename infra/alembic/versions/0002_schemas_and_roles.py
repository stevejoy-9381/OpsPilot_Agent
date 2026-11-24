"""create schemas and roles

Revision ID: 0002_schemas_and_roles
Revises: 0001_baseline
Create Date: 2026-11-24 16:15:00.000000

Creates schemas:
  - app
  - checkpoints

Creates DB roles:
  - opspilot_api (app DML only, no DDL)
  - opspilot_worker (app DML, restricted)
  - opspilot_migrator (DDL)
  - opspilot_checkpointer (checkpoints schema only)
  - broker (action_executions write, action_plans read only; TODO Feature 9)

Passwords sourced from environment variables with fallback dev defaults.
"""

from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_schemas_and_roles"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def get_role_password(env_var: str, default: str) -> str:
    """Retrieve password from environment or use dev fallback."""
    return os.environ.get(env_var, default)


def upgrade() -> None:
    # 1. Create schemas
    op.execute("CREATE SCHEMA IF NOT EXISTS app;")
    op.execute("CREATE SCHEMA IF NOT EXISTS checkpoints;")

    # 2. Passwords for roles
    api_pw = get_role_password("OPSPILOT_API_PASSWORD", "opspilot_api_dev_pw")
    worker_pw = get_role_password("OPSPILOT_WORKER_PASSWORD", "opspilot_worker_dev_pw")
    migrator_pw = get_role_password("OPSPILOT_MIGRATOR_PASSWORD", "opspilot_migrator_dev_pw")
    checkpointer_pw = get_role_password(
        "OPSPILOT_CHECKPOINTER_PASSWORD", "opspilot_checkpointer_dev_pw"
    )
    broker_pw = get_role_password("OPSPILOT_BROKER_PASSWORD", "opspilot_broker_dev_pw")

    # 3. Create roles (DO NOTHING if role already exists)
    roles = [
        ("opspilot_api", api_pw),
        ("opspilot_worker", worker_pw),
        ("opspilot_migrator", migrator_pw),
        ("opspilot_checkpointer", checkpointer_pw),
        ("broker", broker_pw),
    ]

    for role_name, pw in roles:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
                    CREATE ROLE {role_name} WITH LOGIN PASSWORD '{pw}';
                ELSE
                    ALTER ROLE {role_name} WITH LOGIN PASSWORD '{pw}';
                END IF;
            END
            $$;
            """
        )

    # 4. Grants on schemas
    # Schema usage
    op.execute(
        "GRANT USAGE ON SCHEMA app TO opspilot_api, opspilot_worker, opspilot_migrator, broker;"
    )
    op.execute("GRANT ALL ON SCHEMA app TO opspilot_migrator;")

    op.execute(
        "GRANT USAGE, CREATE ON SCHEMA checkpoints TO opspilot_checkpointer, opspilot_migrator;"
    )
    op.execute("GRANT ALL ON SCHEMA checkpoints TO opspilot_migrator;")

    # Ensure checkpointer CANNOT access app schema
    op.execute("REVOKE ALL ON SCHEMA app FROM opspilot_checkpointer;")
    op.execute("REVOKE ALL ON SCHEMA checkpoints FROM opspilot_api, opspilot_worker, broker;")

    # Default privileges for tables created in future migrations
    # Migrator creates tables in app -> grant DML to opspilot_api and opspilot_worker
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO opspilot_api, opspilot_worker;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO opspilot_api, opspilot_worker;"
    )

    # Checkpointer default privileges in checkpoints schema
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints GRANT ALL ON TABLES TO opspilot_checkpointer;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints GRANT ALL ON SEQUENCES TO opspilot_checkpointer;"
    )


def downgrade() -> None:
    # Revoke default privileges
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints REVOKE ALL ON SEQUENCES FROM opspilot_checkpointer;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA checkpoints REVOKE ALL ON TABLES FROM opspilot_checkpointer;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON SEQUENCES FROM opspilot_api, opspilot_worker;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM opspilot_api, opspilot_worker;"
    )

    # Drop schemas (CASCADE will drop tables in them)
    op.execute("DROP SCHEMA IF EXISTS checkpoints CASCADE;")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE;")

    # Drop roles
    for role_name in [
        "broker",
        "opspilot_checkpointer",
        "opspilot_migrator",
        "opspilot_worker",
        "opspilot_api",
    ]:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
                    DROP ROLE {role_name};
                END IF;
            END
            $$;
            """
        )
