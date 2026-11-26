"""add run_leases, approval_idempotency, and runs.created_by_user_id for Feature 8

Revision ID: a1b2c3d4e5f6
Revises: 95e6f7a8b9c0
Create Date: 2026-11-25 21:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "95e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Track run creator for Separation of Duties audit & optional enforcement
    op.add_column(
        "runs",
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="app",
    )

    # 2. Dedicated run_leases table so heartbeats never bump runs.version
    op.execute(
        """
        CREATE TABLE app.run_leases (
            run_id UUID PRIMARY KEY REFERENCES app.runs(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            worker_id VARCHAR(128) NOT NULL,
            lease_token UUID NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            lease_expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX idx_run_leases_expires_at ON app.run_leases (lease_expires_at ASC);
        """
    )

    # 3. Approval idempotency response cache
    op.execute(
        """
        CREATE TABLE app.approval_idempotency (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            plan_id UUID NOT NULL REFERENCES app.action_plans(id) ON DELETE CASCADE,
            reviewer_id UUID NOT NULL REFERENCES app.users(id) ON DELETE RESTRICT,
            idempotency_key VARCHAR(128) NOT NULL,
            decision VARCHAR(32) NOT NULL CHECK (decision IN ('approve', 'reject')),
            submitted_plan_hash CHAR(64) NOT NULL,
            response_status INTEGER NOT NULL,
            response_body JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_approval_idempotency UNIQUE (tenant_id, plan_id, reviewer_id, idempotency_key)
        );
        """
    )

    # 4. Enable RLS on new tenant-scoped tables
    for table in ("run_leases", "approval_idempotency"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_policy_{table} ON app.{table}
            FOR ALL
            USING (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            );
            """
        )

    # 5. Grant DML to opspilot_api and opspilot_worker
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.run_leases, app.approval_idempotency TO opspilot_api, opspilot_worker;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.approval_idempotency;")
    op.execute("DROP TABLE IF EXISTS app.run_leases;")
    op.drop_column("runs", "created_by_user_id", schema="app")
