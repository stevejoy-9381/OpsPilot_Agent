"""add cancel transitions to allowed_transitions

Revision ID: 41f48d91c101
Revises: 33cfa0915ccc
Create Date: 2026-11-24 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '41f48d91c101'
down_revision: Union[str, None] = '33cfa0915ccc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO app.allowed_transitions (from_status, to_status, description)
        VALUES 
            ('queued', 'escalated', 'Run cancelled or failed while queued'),
            ('approved', 'escalated', 'Target drift or plan cancelled before broker pickup')
        ON CONFLICT (from_status, to_status) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM app.allowed_transitions 
        WHERE (from_status = 'queued' AND to_status = 'escalated')
           OR (from_status = 'approved' AND to_status = 'escalated');
        """
    )
