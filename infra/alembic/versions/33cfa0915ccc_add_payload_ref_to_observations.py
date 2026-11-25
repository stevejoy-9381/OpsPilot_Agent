"""add payload_ref to observations

Revision ID: 33cfa0915ccc
Revises: 317a98a10987
Create Date: 2026-11-24 17:29:36.249661

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '33cfa0915ccc'
down_revision: Union[str, None] = '317a98a10987'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.observations
        ADD COLUMN IF NOT EXISTS payload_ref VARCHAR(255) NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.observations
        DROP COLUMN IF EXISTS payload_ref;
        """
    )
