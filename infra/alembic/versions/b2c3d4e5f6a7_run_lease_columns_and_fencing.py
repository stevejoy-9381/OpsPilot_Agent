"""add lease_owner, lease_token, lease_expires_at to app.runs and exempt lease-only updates from version bump

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-11-25 22:25:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        schema="app",
    )
    op.add_column(
        "runs",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        schema="app",
    )
    op.add_column(
        "runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )

    op.execute(
        """
        CREATE INDEX idx_runs_lease_reclaim
        ON app.runs (status, lease_expires_at ASC)
        WHERE status IN ('queued', 'investigating', 'approved', 'verifying');
        """
    )

    # Update runs transition trigger so lease-only heartbeat/claim updates do NOT bump version
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_runs_enforce_transition()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM app.allowed_transitions
                    WHERE from_status = OLD.status AND to_status = NEW.status
                ) THEN
                    RAISE EXCEPTION 'Illegal run status transition from "%" to "%"', OLD.status, NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            IF NEW.status IS NOT DISTINCT FROM OLD.status
               AND NEW.resolution IS NOT DISTINCT FROM OLD.resolution
               AND NEW.remaining_calls IS NOT DISTINCT FROM OLD.remaining_calls
               AND NEW.last_error IS NOT DISTINCT FROM OLD.last_error THEN
                -- Lease/heartbeat-only update: preserve optimistic concurrency version
                NEW.version = OLD.version;
            ELSE
                NEW.version = OLD.version + 1;
            END IF;

            NEW.updated_at = clock_timestamp();

            IF NEW.status IN ('completed', 'escalated') AND OLD.status NOT IN ('completed', 'escalated') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.idx_runs_lease_reclaim;")
    op.drop_column("runs", "lease_expires_at", schema="app")
    op.drop_column("runs", "lease_token", schema="app")
    op.drop_column("runs", "lease_owner", schema="app")
