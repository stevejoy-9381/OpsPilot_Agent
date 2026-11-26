"""add target_id inflight unique index and dispatched->failed transition

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-26 10:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.action_executions
            ADD COLUMN IF NOT EXISTS target_id VARCHAR(64) NULL;

        UPDATE app.action_executions ae
        SET target_id = ap.target_id
        FROM app.action_plans ap
        WHERE ae.plan_id = ap.id AND ae.target_id IS NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_action_executions_inflight_target
            ON app.action_executions (target_id)
            WHERE status IN ('dispatched', 'uncertain');

        INSERT INTO app.execution_transitions (from_status, to_status, description)
        VALUES ('dispatched', 'failed', 'Target returned precondition_failed or 4xx client error')
        ON CONFLICT (from_status, to_status) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM app.execution_transitions WHERE from_status = 'dispatched' AND to_status = 'failed';
        DROP INDEX IF EXISTS app.uq_action_executions_inflight_target;
        ALTER TABLE app.action_executions DROP COLUMN IF EXISTS target_id;
        """
    )
