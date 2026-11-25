"""add failed status and transitions

Revision ID: 42a1b2c3d4e5
Revises: 41f48d91c101
Create Date: 2026-11-24 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '42a1b2c3d4e5'
down_revision: Union[str, None] = '41f48d91c101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update runs status CHECK constraint to include 'failed'
    op.execute(
        """
        ALTER TABLE app.runs DROP CONSTRAINT IF EXISTS runs_status_check;
        ALTER TABLE app.runs ADD CONSTRAINT runs_status_check 
            CHECK (status IN ('queued', 'investigating', 'needs_review', 'approved', 'executing', 'verifying', 'completed', 'escalated', 'failed'));
        """
    )

    # 2. Add failed transitions to allowed_transitions table
    op.execute(
        """
        INSERT INTO app.allowed_transitions (from_status, to_status, description)
        VALUES 
            ('queued', 'failed', 'Startup crash or malformed incident fixture'),
            ('investigating', 'failed', 'Agent process error or run timeout'),
            ('executing', 'failed', 'Broker host crash or fatal broker error')
        ON CONFLICT (from_status, to_status) DO NOTHING;
        """
    )

    # 3. Update fn_runs_enforce_transition to set finished_at on terminal 'failed' status
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_runs_enforce_transition()
        RETURNS TRIGGER AS $$
        BEGIN
            -- Allow same-status updates (e.g. updating remaining_calls, last_error, finished_at)
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF NOT EXISTS (
                    SELECT 1 FROM app.allowed_transitions
                    WHERE from_status = OLD.status AND to_status = NEW.status
                ) THEN
                    RAISE EXCEPTION 'Illegal run status transition from "%" to "%"', OLD.status, NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            -- Optimistic locking: auto-increment version and update timestamp
            NEW.version = OLD.version + 1;
            NEW.updated_at = clock_timestamp();

            IF NEW.status IN ('completed', 'escalated', 'failed') AND OLD.status NOT IN ('completed', 'escalated', 'failed') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        -- Also ensure fn_action_plans_verify_plan_hash produces whitespace-free canonical JSON
        CREATE OR REPLACE FUNCTION app.fn_action_plans_verify_plan_hash()
        RETURNS TRIGGER AS $$
        DECLARE
            v_canonical_json TEXT;
            v_computed_hash TEXT;
            v_sorted_evidence TEXT;
        BEGIN
            SELECT COALESCE('[' || string_agg(to_json(elem)::text, ',' ORDER BY elem) || ']', '[]')
            INTO v_sorted_evidence
            FROM unnest(NEW.rationale_evidence_ids) AS elem;

            IF v_sorted_evidence IS NULL THEN
                v_sorted_evidence := '[]';
            END IF;

            v_canonical_json := concat(
                '{"action":', to_json(lower(trim(NEW.action))),
                ',"expected_version":', CASE WHEN NEW.expected_version IS NOT NULL THEN to_json(trim(NEW.expected_version)) ELSE 'null' END,
                ',"rationale_evidence_ids":', v_sorted_evidence,
                ',"rollback_route":', CASE WHEN NEW.rollback_route IS NOT NULL THEN to_json(trim(NEW.rollback_route)) ELSE 'null' END,
                ',"service":', CASE WHEN NEW.service IS NOT NULL THEN to_json(lower(trim(NEW.service))) ELSE 'null' END,
                '}'
            );

            v_computed_hash := encode(digest(v_canonical_json, 'sha256'), 'hex');

            IF lower(trim(NEW.plan_hash)) <> v_computed_hash THEN
                RAISE EXCEPTION 'Plan hash mismatch on insert: provided "%", recomputed "%"', NEW.plan_hash, v_computed_hash
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM app.allowed_transitions 
        WHERE to_status = 'failed';

        SET session_replication_role = replica;
        DELETE FROM app.runs 
        WHERE status = 'failed';
        SET session_replication_role = DEFAULT;
        """
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
            END IF;

            NEW.version = OLD.version + 1;
            NEW.updated_at = clock_timestamp();

            IF NEW.status IN ('completed', 'escalated') AND OLD.status NOT IN ('completed', 'escalated') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        ALTER TABLE app.runs DROP CONSTRAINT IF EXISTS runs_status_check;
        ALTER TABLE app.runs ADD CONSTRAINT runs_status_check 
            CHECK (status IN ('queued', 'investigating', 'needs_review', 'approved', 'executing', 'verifying', 'completed', 'escalated'));
        """
    )
