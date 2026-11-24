"""create core domain tables

Revision ID: 0003_core_domain_tables
Revises: 0002_schemas_and_roles
Create Date: 2026-11-24 16:20:00.000000

Creates core domain tables in app schema:
  - app.tenants
  - app.users
  - app.sessions
  - app.incidents
  - app.runs
  - app.observations
  - app.hypotheses
  - app.diagnoses
  - app.action_plans
  - app.approvals
  - app.action_executions
  - app.verification_results
  - app.run_events

Uses UUID primary keys (uuidv4 via gen_random_uuid()), TIMESTAMPTZ everywhere,
JSONB with jsonb_typeof CHECKs, and exact domain constraints.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_core_domain_tables"
down_revision: Union[str, None] = "0002_schemas_and_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Ensure pgcrypto extension for gen_random_uuid() (standard in PG13+)
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')

    # 1. app.tenants
    op.execute(
        """
        CREATE TABLE app.tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        """
    )

    # 2. app.users
    op.execute(
        """
        CREATE TABLE app.users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            email VARCHAR(255) NOT NULL,
            role VARCHAR(32) NOT NULL CHECK (role IN ('viewer', 'reviewer', 'admin')),
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_users_tenant_email UNIQUE (tenant_id, email)
        );
        """
    )

    # 3. app.sessions
    op.execute(
        """
        CREATE TABLE app.sessions (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        """
    )

    # 4. app.incidents
    op.execute(
        """
        CREATE TABLE app.incidents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            alert_id VARCHAR(64) NOT NULL,
            source VARCHAR(32) NOT NULL CHECK (source IN ('alert', 'seeded_case')),
            service VARCHAR(64) NOT NULL,
            severity VARCHAR(32) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
            alert_payload JSONB NOT NULL CHECK (jsonb_typeof(alert_payload) = 'object'),
            opened_at TIMESTAMPTZ NOT NULL,
            status_summary VARCHAR(32) NOT NULL DEFAULT 'open' CHECK (status_summary IN ('open', 'mitigated', 'resolved', 'escalated')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX idx_incidents_tenant_opened ON app.incidents (tenant_id, status_summary, opened_at DESC);
        """
    )

    # 5. app.runs
    op.execute(
        """
        CREATE TABLE app.runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            incident_id UUID NOT NULL REFERENCES app.incidents(id) ON DELETE RESTRICT,
            case_id VARCHAR(64) NULL,
            idempotency_key VARCHAR(128) NULL,
            status VARCHAR(32) NOT NULL CHECK (status IN ('queued', 'investigating', 'needs_review', 'approved', 'executing', 'verifying', 'completed', 'escalated')),
            version INTEGER NOT NULL DEFAULT 1,
            remaining_calls INTEGER NOT NULL DEFAULT 8 CHECK (remaining_calls >= 0 AND remaining_calls <= max_calls),
            max_calls INTEGER NOT NULL DEFAULT 8 CHECK (max_calls > 0),
            last_error JSONB NULL CHECK (last_error IS NULL OR jsonb_typeof(last_error) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            finished_at TIMESTAMPTZ NULL,
            CONSTRAINT uq_runs_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
        );
        CREATE INDEX idx_runs_tenant_status ON app.runs (tenant_id, status, updated_at DESC);
        """
    )

    # 6. app.observations
    op.execute(
        """
        CREATE TABLE app.observations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            evidence_id VARCHAR(64) NOT NULL,
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            tool VARCHAR(64) NOT NULL CHECK (tool IN ('get_metrics', 'search_logs', 'get_trace', 'list_deploys', 'get_deploy_diff')),
            source_id VARCHAR(128) NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL,
            window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL,
            args JSONB NOT NULL CHECK (jsonb_typeof(args) = 'object'),
            summary TEXT NOT NULL,
            payload JSONB NOT NULL,
            source_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            content_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_observations_run_evidence UNIQUE (run_id, evidence_id)
        );
        CREATE INDEX idx_observations_run_captured ON app.observations (run_id, captured_at ASC);
        """
    )

    # 7. app.hypotheses
    op.execute(
        """
        CREATE TABLE app.hypotheses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            statement TEXT NOT NULL,
            category VARCHAR(64) NOT NULL CHECK (category IN ('bad_deploy', 'dependency_failure', 'false_alarm', 'unknown')),
            mechanism VARCHAR(64) NOT NULL,
            service VARCHAR(64) NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'supported', 'refuted')),
            supporting_evidence_ids TEXT[] NOT NULL DEFAULT '{}',
            contrary_evidence_ids TEXT[] NOT NULL DEFAULT '{}',
            confidence NUMERIC(3, 2) NOT NULL CHECK (confidence >= 0.00 AND confidence <= 1.00),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX idx_hypotheses_run ON app.hypotheses (run_id, status);
        """
    )

    # 8. app.diagnoses
    op.execute(
        """
        CREATE TABLE app.diagnoses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL UNIQUE REFERENCES app.runs(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            root_cause_service VARCHAR(64) NULL,
            root_cause_category VARCHAR(64) NOT NULL CHECK (root_cause_category IN ('bad_deploy', 'dependency_failure', 'false_alarm', 'unknown')),
            mechanism VARCHAR(64) NOT NULL CHECK (mechanism IN ('connection_pool_exhaustion', 'cache_bypass_latency', 'dependency_timeout', 'none', 'unknown')),
            culprit_deploy_id VARCHAR(64) NULL,
            evidence_ids TEXT[] NOT NULL DEFAULT '{}',
            confidence NUMERIC(3, 2) NOT NULL CHECK (confidence >= 0.00 AND confidence <= 1.00),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        """
    )

    # 9. app.action_plans
    op.execute(
        """
        CREATE TABLE app.action_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            action VARCHAR(32) NOT NULL CHECK (action IN ('rollback_service', 'restart_service', 'no_action', 'escalate')),
            service VARCHAR(64) NULL,
            expected_version VARCHAR(64) NULL,
            rationale_evidence_ids TEXT[] NOT NULL DEFAULT '{}',
            rollback_route VARCHAR(255) NULL,
            plan_hash CHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected', 'expired', 'superseded', 'executed', 'failed')),
            notes VARCHAR(300) NOT NULL DEFAULT '',
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT chk_plan_action_service_consistency CHECK (
                (action IN ('rollback_service', 'restart_service') AND service IS NOT NULL)
                OR
                (action IN ('no_action', 'escalate') AND service IS NULL)
            )
        );
        CREATE INDEX idx_action_plans_run_status ON app.action_plans (run_id, status);
        """
    )

    # 10. app.approvals
    op.execute(
        """
        CREATE TABLE app.approvals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL REFERENCES app.action_plans(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            reviewer_id UUID NOT NULL REFERENCES app.users(id) ON DELETE RESTRICT,
            decision VARCHAR(32) NOT NULL CHECK (decision IN ('approve', 'reject')),
            submitted_plan_hash CHAR(64) NOT NULL,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            reason TEXT NOT NULL DEFAULT '',
            CONSTRAINT uq_approvals_plan_reviewer UNIQUE (plan_id, reviewer_id)
        );
        """
    )

    # 11. app.action_executions
    op.execute(
        """
        CREATE TABLE app.action_executions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL UNIQUE REFERENCES app.action_plans(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            action_key VARCHAR(128) NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL CHECK (status IN ('queued', 'dispatched', 'acknowledged', 'uncertain', 'reconciled', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            target_state_before JSONB NULL CHECK (target_state_before IS NULL OR jsonb_typeof(target_state_before) = 'object'),
            target_state_after JSONB NULL CHECK (target_state_after IS NULL OR jsonb_typeof(target_state_after) = 'object'),
            dispatched_at TIMESTAMPTZ NULL,
            completed_at TIMESTAMPTZ NULL,
            broker_response JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX idx_action_executions_status ON app.action_executions (status, created_at ASC);
        """
    )

    # 12. app.verification_results
    op.execute(
        """
        CREATE TABLE app.verification_results (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE RESTRICT,
            execution_id UUID NOT NULL UNIQUE REFERENCES app.action_executions(id) ON DELETE RESTRICT,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            pre_metrics JSONB NOT NULL CHECK (jsonb_typeof(pre_metrics) = 'object'),
            post_metrics JSONB NOT NULL CHECK (jsonb_typeof(post_metrics) = 'object'),
            probe_result JSONB NOT NULL CHECK (jsonb_typeof(probe_result) = 'object'),
            verdict VARCHAR(32) NOT NULL CHECK (verdict IN ('recovered', 'not_recovered', 'inconclusive')),
            window_seconds INTEGER NOT NULL DEFAULT 30,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        """
    )

    # 13. app.run_events
    op.execute(
        """
        CREATE TABLE app.run_events (
            id BIGSERIAL PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            seq INTEGER NOT NULL,
            type VARCHAR(64) NOT NULL,
            actor VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_run_events_run_seq UNIQUE (run_id, seq)
        );
        CREATE INDEX idx_run_events_run_seq ON app.run_events (run_id, seq ASC);
        """
    )

    # 14. Plan Immutability Trigger: prevent altering core fields after insertion
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_plans_prevent_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.action <> OLD.action OR
               NEW.service IS DISTINCT FROM OLD.service OR
               NEW.expected_version IS DISTINCT FROM OLD.expected_version OR
               NEW.rationale_evidence_ids <> OLD.rationale_evidence_ids OR
               NEW.rollback_route IS DISTINCT FROM OLD.rollback_route OR
               NEW.plan_hash <> OLD.plan_hash THEN
                RAISE EXCEPTION 'Action plan core proposal fields are immutable after insertion';
            END IF;
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_action_plans_prevent_mutation
        BEFORE UPDATE ON app.action_plans
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_action_plans_prevent_mutation();
        """
    )

    # 15. Broker role permissions: action_executions DML and action_plans read only (TODO: finalize Feature 9)
    op.execute("GRANT SELECT ON app.action_plans TO broker;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.action_executions TO broker;")
    op.execute("GRANT SELECT ON app.tenants, app.runs TO broker;")


def downgrade() -> None:
    # 1. Drop trigger and function
    op.execute("DROP TRIGGER IF EXISTS trg_action_plans_prevent_mutation ON app.action_plans;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_action_plans_prevent_mutation();")

    # 2. Revoke broker table permissions
    op.execute("REVOKE ALL ON app.action_executions FROM broker;")
    op.execute("REVOKE ALL ON app.action_plans FROM broker;")
    op.execute("REVOKE ALL ON app.tenants, app.runs FROM broker;")

    # 3. Drop tables in reverse topological order
    tables = [
        "app.run_events",
        "app.verification_results",
        "app.action_executions",
        "app.approvals",
        "app.action_plans",
        "app.diagnoses",
        "app.hypotheses",
        "app.observations",
        "app.runs",
        "app.incidents",
        "app.sessions",
        "app.users",
        "app.tenants",
    ]
    for table in tables:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
