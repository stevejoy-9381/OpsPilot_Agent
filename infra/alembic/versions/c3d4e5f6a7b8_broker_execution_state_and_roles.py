"""broker execution state machine, events, and opspilot_broker role

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-25 22:55:00.000000

Implements Feature 9 (DB side):
1. Extends app.action_executions with lease, timing, refusal_code, last_error, target_response,
   and extended status CHECK constraint.
2. Creates app.execution_transitions table and BEFORE UPDATE trigger enforcing legal transitions
   and immutability of id, plan_id, tenant_id, action_key.
3. Creates append-only app.execution_events table with BEFORE UPDATE OR DELETE trigger.
4. Creates/configures opspilot_broker role with least-privilege grants, revokes UPDATE/DELETE
   on app.action_executions from opspilot_api and opspilot_worker, and adds broker RLS policies.
5. Ensures ('approved', 'failed') is in app.allowed_transitions.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    broker_pw = os.environ.get("OPSPILOT_BROKER_PASSWORD", "opspilot_broker_dev_pw")

    # 1. Extend app.action_executions columns and status CHECK constraint
    op.execute(
        """
        ALTER TABLE app.action_executions
            ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128) NULL,
            ADD COLUMN IF NOT EXISTS lease_token UUID NULL,
            ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS refusal_code VARCHAR(64) NULL,
            ADD COLUMN IF NOT EXISTS last_error JSONB NULL,
            ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS target_response JSONB NULL;

        ALTER TABLE app.action_executions DROP CONSTRAINT IF EXISTS action_executions_last_error_check;
        ALTER TABLE app.action_executions ADD CONSTRAINT action_executions_last_error_check
            CHECK (last_error IS NULL OR jsonb_typeof(last_error) = 'object');

        ALTER TABLE app.action_executions DROP CONSTRAINT IF EXISTS action_executions_status_check;
        ALTER TABLE app.action_executions ADD CONSTRAINT action_executions_status_check
            CHECK (status IN (
                'queued', 'claimed', 'dispatched', 'acknowledged',
                'uncertain', 'reconciled', 'failed', 'refused', 'succeeded'
            ));

        CREATE INDEX IF NOT EXISTS idx_action_executions_lease_reclaim
            ON app.action_executions (status, lease_expires_at ASC)
            WHERE status IN ('claimed', 'dispatched', 'uncertain');
        """
    )

    # 2. Transition table app.execution_transitions
    op.execute(
        """
        CREATE TABLE app.execution_transitions (
            from_status VARCHAR(32) NOT NULL,
            to_status VARCHAR(32) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (from_status, to_status)
        );
        """
    )

    execution_transitions = [
        ("queued", "claimed", "Broker claims queued execution with lease"),
        ("claimed", "dispatched", "Broker passes B1-B10 and dispatches action"),
        ("claimed", "refused", "Broker pre-dispatch check B1-B10 failed"),
        ("claimed", "failed", "Unrecoverable broker error before dispatch"),
        ("dispatched", "acknowledged", "Target returned applied or already_applied"),
        ("dispatched", "refused", "Target returned precondition_failed"),
        ("dispatched", "uncertain", "Timeout, connection reset, 5xx without body, or lease reclaim"),
        ("acknowledged", "succeeded", "Execution handoff to verification completed"),
        ("uncertain", "reconciled", "Reconcile confirmed action was applied on target"),
        ("uncertain", "dispatched", "Reconcile confirmed not_applied; retrying with same action_key"),
        ("uncertain", "failed", "Reconcile returned still_unknown or max attempts exhausted"),
        ("reconciled", "succeeded", "Execution handoff to verification completed after reconcile"),
    ]
    for from_s, to_s, desc in execution_transitions:
        op.execute(
            f"""
            INSERT INTO app.execution_transitions (from_status, to_status, description)
            VALUES ('{from_s}', '{to_s}', '{desc}')
            ON CONFLICT (from_status, to_status) DO NOTHING;
            """
        )

    # 3. Trigger enforcing app.execution_transitions & core field immutability on app.action_executions
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_executions_enforce_transition()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.action_key IS DISTINCT FROM OLD.action_key THEN
                RAISE EXCEPTION 'Immutable execution fields (id, plan_id, tenant_id, action_key) cannot be modified'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF current_user IN ('opspilot_api', 'opspilot_worker') THEN
                    RAISE EXCEPTION 'Role "%" is not permitted to update action_executions status', current_user
                        USING ERRCODE = 'insufficient_privilege';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM app.execution_transitions
                    WHERE from_status = OLD.status AND to_status = NEW.status
                ) THEN
                    RAISE EXCEPTION 'Illegal execution status transition from "%" to "%"', OLD.status, NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_action_executions_enforce_transition
        BEFORE UPDATE ON app.action_executions
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_action_executions_enforce_transition();
        """
    )

    # 4. Append-only app.execution_events table
    op.execute(
        """
        CREATE TABLE app.execution_events (
            id BIGSERIAL PRIMARY KEY,
            execution_id UUID NOT NULL REFERENCES app.action_executions(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            seq INTEGER NOT NULL,
            type VARCHAR(64) NOT NULL,
            actor VARCHAR(64) NOT NULL DEFAULT 'broker',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_execution_events_exec_seq UNIQUE (execution_id, seq)
        );
        CREATE INDEX idx_execution_events_exec_seq ON app.execution_events (execution_id, seq ASC);

        CREATE TRIGGER trg_execution_events_append_only
        BEFORE UPDATE OR DELETE ON app.execution_events
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_reject_update_and_delete();

        ALTER TABLE app.execution_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE app.execution_events FORCE ROW LEVEL SECURITY;

        CREATE POLICY tenant_isolation_policy_execution_events ON app.execution_events
        FOR ALL
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        );
        """
    )

    # 5. Add missing run transition ('approved', 'failed') for pre-dispatch fatal broker errors
    op.execute(
        """
        INSERT INTO app.allowed_transitions (from_status, to_status, description)
        VALUES ('approved', 'failed', 'Broker fatal error before dispatch')
        ON CONFLICT (from_status, to_status) DO NOTHING;
        """
    )

    # 6. Create and configure opspilot_broker role with least-privilege grants
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'opspilot_broker') THEN
                CREATE ROLE opspilot_broker WITH LOGIN PASSWORD '{broker_pw}';
            ELSE
                ALTER ROLE opspilot_broker WITH LOGIN PASSWORD '{broker_pw}';
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        GRANT USAGE ON SCHEMA app TO opspilot_broker;
        REVOKE ALL ON SCHEMA checkpoints FROM opspilot_broker;
        REVOKE ALL ON ALL TABLES IN SCHEMA app FROM opspilot_broker;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA app FROM opspilot_broker;

        -- Read-only access to tables needed for B1-B10 pre-dispatch verification
        GRANT SELECT ON app.action_plans, app.approvals, app.execution_transitions, app.allowed_transitions TO opspilot_broker;
        GRANT SELECT (id, tenant_id, status, version, last_error) ON app.runs TO opspilot_broker;
        GRANT UPDATE (status, version, last_error, updated_at, finished_at) ON app.runs TO opspilot_broker;
        GRANT SELECT (id, tenant_id, role) ON app.users TO opspilot_broker;

        -- Broker execution & audit tables
        GRANT SELECT, UPDATE ON app.action_executions TO opspilot_broker;
        GRANT SELECT, INSERT ON app.execution_events TO opspilot_broker;
        GRANT SELECT, INSERT ON app.run_events TO opspilot_broker;
        GRANT USAGE, SELECT ON SEQUENCE app.execution_events_id_seq, app.run_events_id_seq TO opspilot_broker;

        -- API and worker can INSERT queued executions via ApprovalService, but NEVER update/delete them
        REVOKE UPDATE, DELETE ON app.action_executions FROM opspilot_api, opspilot_worker;
        REVOKE UPDATE, DELETE ON app.execution_events FROM opspilot_api, opspilot_worker;
        GRANT SELECT ON app.execution_transitions TO opspilot_api, opspilot_worker;

        -- RLS policies for opspilot_broker:
        -- Broker must see all tenants' queued/claimed/dispatched/uncertain executions to claim work,
        -- while trigger fn_action_executions_enforce_transition forbids mutating tenant_id on UPDATE
        -- and execution_events WITH CHECK requires tenant_id to match the execution row.
        CREATE POLICY broker_action_executions_policy ON app.action_executions
        FOR ALL
        TO opspilot_broker
        USING (true)
        WITH CHECK (true);

        CREATE POLICY broker_execution_events_policy ON app.execution_events
        FOR ALL
        TO opspilot_broker
        USING (true)
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM app.action_executions ae
                WHERE ae.id = execution_id AND ae.tenant_id = tenant_id
            )
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS broker_execution_events_policy ON app.execution_events;
        DROP POLICY IF EXISTS broker_action_executions_policy ON app.action_executions;

        REVOKE ALL ON ALL TABLES IN SCHEMA app FROM opspilot_broker;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA app FROM opspilot_broker;
        REVOKE USAGE ON SCHEMA app FROM opspilot_broker;

        GRANT UPDATE, DELETE ON app.action_executions TO opspilot_api, opspilot_worker;

        DROP TRIGGER IF EXISTS trg_execution_events_append_only ON app.execution_events;
        DROP TABLE IF EXISTS app.execution_events;

        DROP TRIGGER IF EXISTS trg_action_executions_enforce_transition ON app.action_executions;
        DROP FUNCTION IF EXISTS app.fn_action_executions_enforce_transition();
        DROP TABLE IF EXISTS app.execution_transitions;

        DROP INDEX IF EXISTS app.idx_action_executions_lease_reclaim;

        ALTER TABLE app.action_executions DROP CONSTRAINT IF EXISTS action_executions_last_error_check;
        ALTER TABLE app.action_executions DROP CONSTRAINT IF EXISTS action_executions_status_check;
        ALTER TABLE app.action_executions ADD CONSTRAINT action_executions_status_check
            CHECK (status IN ('queued', 'dispatched', 'acknowledged', 'uncertain', 'reconciled', 'failed'));

        ALTER TABLE app.action_executions
            DROP COLUMN IF EXISTS target_response,
            DROP COLUMN IF EXISTS reconciled_at,
            DROP COLUMN IF EXISTS acknowledged_at,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS refusal_code,
            DROP COLUMN IF EXISTS heartbeat_at,
            DROP COLUMN IF EXISTS lease_expires_at,
            DROP COLUMN IF EXISTS lease_token,
            DROP COLUMN IF EXISTS lease_owner;

        DELETE FROM app.allowed_transitions WHERE from_status = 'approved' AND to_status = 'failed';
        """
    )
