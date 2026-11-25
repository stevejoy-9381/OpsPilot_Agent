"""add outcome fields to diagnoses and false_alarm completed transition

Revision ID: 73c4d5e6f7a8
Revises: 62b3c4d5e6f7
Create Date: 2026-11-25 16:15:00.000000

Adds to app.diagnoses:
  - outcome_kind VARCHAR(32) NOT NULL DEFAULT 'unknown'
  - outcome_reason VARCHAR(64) NOT NULL DEFAULT 'concluded_by_model'
  - model_confidence VARCHAR(32) NULL
  - capped_confidence NUMERIC(3, 2) NULL
  - ledger_summary JSONB NULL
  - what_would_change TEXT NULL
  - gate_rejection_count INTEGER NOT NULL DEFAULT 0

Adds to app.allowed_transitions:
  - ('investigating', 'completed', 'False alarm verified with no_action')
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "73c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "62b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "diagnoses",
        sa.Column("outcome_kind", sa.String(length=32), nullable=False, server_default="unknown"),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("outcome_reason", sa.String(length=64), nullable=False, server_default="concluded_by_model"),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("model_confidence", sa.String(length=32), nullable=True),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("capped_confidence", sa.Numeric(3, 2), nullable=True),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("ledger_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("what_would_change", sa.Text(), nullable=True),
        schema="app",
    )
    op.add_column(
        "diagnoses",
        sa.Column("gate_rejection_count", sa.Integer(), nullable=False, server_default="0"),
        schema="app",
    )

    op.execute(
        """
        INSERT INTO app.allowed_transitions (from_status, to_status, description)
        VALUES ('investigating', 'completed', 'False alarm verified with no_action')
        ON CONFLICT (from_status, to_status) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM app.allowed_transitions
        WHERE from_status = 'investigating' AND to_status = 'completed';
        """
    )
    op.drop_column("diagnoses", "gate_rejection_count", schema="app")
    op.drop_column("diagnoses", "what_would_change", schema="app")
    op.drop_column("diagnoses", "ledger_summary", schema="app")
    op.drop_column("diagnoses", "capped_confidence", schema="app")
    op.drop_column("diagnoses", "model_confidence", schema="app")
    op.drop_column("diagnoses", "outcome_reason", schema="app")
    op.drop_column("diagnoses", "outcome_kind", schema="app")
