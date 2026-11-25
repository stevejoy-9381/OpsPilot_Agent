"""add status to diagnoses

Revision ID: 51a2b3c4d5e6
Revises: 42a1b2c3d4e5
Create Date: 2026-11-24 22:15:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '51a2b3c4d5e6'
down_revision: Union[str, None] = '42a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.diagnoses
        ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'draft';

        ALTER TABLE app.runs
        ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS worker_id VARCHAR(64) NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.runs
        DROP COLUMN IF EXISTS worker_id,
        DROP COLUMN IF EXISTS heartbeat_at;

        ALTER TABLE app.diagnoses
        DROP COLUMN IF EXISTS status;
        """
    )
