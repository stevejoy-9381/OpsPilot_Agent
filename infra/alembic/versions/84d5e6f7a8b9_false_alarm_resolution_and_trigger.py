"""enforce false_alarm investigating->completed trigger guard and add runs.resolution

Revision ID: 84d5e6f7a8b9
Revises: 73c4d5e6f7a8
Create Date: 2026-11-25 19:15:00.000000

1. Adds `resolution` column to `app.runs` so `false_alarm_closed` runs can be
   excluded from recovery metrics.
2. Updates `app.fn_runs_enforce_transition()` so `investigating -> completed`
   is permitted ONLY when `app.diagnoses` contains a gate-passed `false_alarm`
   diagnosis (`root_cause_category = 'false_alarm'`, `outcome_kind = 'diagnosed'`,
   `outcome_reason = 'false_alarm_no_action'`) for that run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "84d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "73c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("resolution", sa.String(length=64), nullable=True),
        schema="app",
    )
    op.create_check_constraint(
        "chk_runs_resolution",
        "runs",
        "resolution IS NULL OR resolution IN ('recovered', 'false_alarm_closed', 'escalated', 'failed')",
        schema="app",
    )

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

                -- Guard investigating -> completed: allowed ONLY for a gate-passed false_alarm diagnosis
                IF OLD.status = 'investigating' AND NEW.status = 'completed' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM app.diagnoses d
                        WHERE d.run_id = NEW.id
                          AND d.root_cause_category = 'false_alarm'
                          AND d.outcome_kind = 'diagnosed'
                          AND d.outcome_reason = 'false_alarm_no_action'
                    ) THEN
                        RAISE EXCEPTION 'Transition investigating -> completed requires a gate-passed false_alarm diagnosis'
                            USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
            END IF;

            NEW.version = OLD.version + 1;
            NEW.updated_at = clock_timestamp();

            IF NEW.status IN ('completed', 'escalated', 'failed')
               AND OLD.status NOT IN ('completed', 'escalated', 'failed') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
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

            NEW.version = OLD.version + 1;
            NEW.updated_at = clock_timestamp();

            IF NEW.status IN ('completed', 'escalated', 'failed')
               AND OLD.status NOT IN ('completed', 'escalated', 'failed') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.drop_constraint("chk_runs_resolution", "runs", schema="app", type_="check")
    op.drop_column("runs", "resolution", schema="app")
