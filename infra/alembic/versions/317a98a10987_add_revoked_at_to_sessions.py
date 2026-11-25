"""add revoked_at to sessions

Revision ID: 317a98a10987
Revises: 0004_db_invariants
Create Date: 2026-11-24 17:14:59.851899

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '317a98a10987'
down_revision: Union[str, None] = '0004_db_invariants'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.sessions
        ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.sessions
        DROP COLUMN IF EXISTS revoked_at;
        """
    )
