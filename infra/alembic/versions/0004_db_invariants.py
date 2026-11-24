"""enforce critical invariants in postgres

Revision ID: 0004_db_invariants
Revises: 0003_core_domain_tables
Create Date: 2026-11-24 16:40:00.000000

Implements:
1. Row-Level Security (RLS) on every tenant-scoped table:
   - USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
   - WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
   - FORCE ROW LEVEL SECURITY enabled so table owners are bound.
   - opspilot_migrator granted BYPASSRLS for maintenance/migrations.
2. app.allowed_transitions table seeded from outcome contract.
3. Trigger on app.runs:
   - Validates (OLD.status, NEW.status) against app.allowed_transitions.
   - Auto-increments version and sets updated_at.
4. Plan immutability trigger on app.action_plans:
   - Rejects UPDATE of action, service, expected_version, rationale_evidence_ids,
     rollback_route, plan_hash, run_id, tenant_id.
   - Restricts status transitions.
5. Append-only triggers:
   - app.run_events: rejects UPDATE and DELETE.
   - app.observations: rejects UPDATE and DELETE.
6. Delete rejection triggers:
   - app.approvals: rejects DELETE.
   - app.action_executions: rejects DELETE.
7. Plan hash verification trigger on app.action_plans:
   - Recomputes canonical plan_hash using pgcrypto digest() on canonical JSON
     and verifies exact equality with NEW.plan_hash on INSERT.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_db_invariants"
down_revision: Union[str, None] = "0003_core_domain_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = [
    "users",
    "sessions",
    "incidents",
    "runs",
    "observations",
    "hypotheses",
    "diagnoses",
    "action_plans",
    "approvals",
    "action_executions",
    "verification_results",
    "run_events",
]


def upgrade() -> None:
    # 0. Allow opspilot_migrator to bypass RLS for administrative migrations
    op.execute("ALTER ROLE opspilot_migrator BYPASSRLS;")

    # 1. RLS on every tenant-scoped table
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_policy_{table} ON app.{table}
            FOR ALL
            USING (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            );
            """
        )

    # 2. app.allowed_transitions seeded from outcome contract
    op.execute(
        """
        CREATE TABLE app.allowed_transitions (
            from_status VARCHAR(32) NOT NULL,
            to_status VARCHAR(32) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (from_status, to_status)
        );
        """
    )
    # Grant read access on transitions to api and worker
    op.execute("GRANT SELECT ON app.allowed_transitions TO opspilot_api, opspilot_worker;")

    # Seed transitions from docs/outcome_contract.md:
    transitions = [
        ("queued", "investigating", "Worker lease acquired"),
        ("investigating", "needs_review", "Plan proposed by agent"),
        ("investigating", "escalated", "Stop condition / budget exhausted"),
        ("needs_review", "approved", "Human review approval"),
        ("needs_review", "escalated", "Human rejection or plan expired"),
        ("approved", "executing", "Broker dispatch"),
        ("executing", "verifying", "Broker execution acknowledged"),
        ("executing", "escalated", "Broker execution failed / uncertain"),
        ("verifying", "completed", "Post-recovery verification passed"),
        ("verifying", "escalated", "Verification probe failed / continued SLA breach"),
        ("escalated", "investigating", "Human operator requests re-investigation"),
    ]
    for from_s, to_s, desc in transitions:
        op.execute(
            f"""
            INSERT INTO app.allowed_transitions (from_status, to_status, description)
            VALUES ('{from_s}', '{to_s}', '{desc}');
            """
        )

    # 3. Runs transition trigger: validates status, increments version, sets updated_at
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

            IF NEW.status IN ('completed', 'escalated') AND OLD.status NOT IN ('completed', 'escalated') THEN
                NEW.finished_at = clock_timestamp();
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_runs_enforce_transition
        BEFORE UPDATE ON app.runs
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_runs_enforce_transition();
        """
    )

    # 4. Plan immutability trigger on action_plans
    # (Replaces preliminary trigger from 0003 with comprehensive field locking)
    op.execute("DROP TRIGGER IF EXISTS trg_action_plans_prevent_mutation ON app.action_plans;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_action_plans_prevent_mutation();")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_plans_enforce_immutability()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.action <> OLD.action OR
               NEW.service IS DISTINCT FROM OLD.service OR
               NEW.expected_version IS DISTINCT FROM OLD.expected_version OR
               NEW.rationale_evidence_ids <> OLD.rationale_evidence_ids OR
               NEW.rollback_route IS DISTINCT FROM OLD.rollback_route OR
               NEW.plan_hash <> OLD.plan_hash OR
               NEW.run_id <> OLD.run_id OR
               NEW.tenant_id <> OLD.tenant_id THEN
                RAISE EXCEPTION 'Immutable plan fields (action, service, expected_version, rationale_evidence_ids, rollback_route, plan_hash, run_id, tenant_id) cannot be modified'
                    USING ERRCODE = 'check_violation';
            END IF;

            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_action_plans_enforce_immutability
        BEFORE UPDATE ON app.action_plans
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_action_plans_enforce_immutability();
        """
    )

    # 5. Append-only triggers on run_events and observations (reject UPDATE and DELETE)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_reject_update_and_delete()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Table "%" is strictly append-only; updates and deletes are forbidden', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_run_events_append_only
        BEFORE UPDATE OR DELETE ON app.run_events
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_reject_update_and_delete();

        CREATE TRIGGER trg_observations_append_only
        BEFORE UPDATE OR DELETE ON app.observations
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_reject_update_and_delete();
        """
    )

    # 6. Reject DELETE triggers on approvals and action_executions
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_reject_delete()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Deleting records from "%" is strictly forbidden', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_approvals_reject_delete
        BEFORE DELETE ON app.approvals
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_reject_delete();

        CREATE TRIGGER trg_action_executions_reject_delete
        BEFORE DELETE ON app.action_executions
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_reject_delete();
        """
    )

    # 7. Plan hash verification trigger on INSERT into action_plans
    # Recomputes canonical hash using pgcrypto digest(..., 'sha256') and verifies match with NEW.plan_hash
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_plans_verify_plan_hash()
        RETURNS TRIGGER AS $$
        DECLARE
            v_canonical_json TEXT;
            v_computed_hash TEXT;
            v_sorted_evidence TEXT;
        BEGIN
            -- Produce canonical JSON representation matching apps.api.models.hashing.compute_plan_hash:
            -- 1. sorted evidence array with no whitespace
            SELECT COALESCE('[' || string_agg(to_json(elem)::text, ',' ORDER BY elem) || ']', '[]')
            INTO v_sorted_evidence
            FROM unnest(NEW.rationale_evidence_ids) AS elem;

            IF v_sorted_evidence IS NULL THEN
                v_sorted_evidence := '[]';
            END IF;

            -- 2. Build canonical json with sorted keys and no whitespace
            v_canonical_json := concat(
                '{"action":', to_json(lower(trim(NEW.action))),
                ',"expected_version":', CASE WHEN NEW.expected_version IS NOT NULL THEN to_json(trim(NEW.expected_version)) ELSE 'null' END,
                ',"rationale_evidence_ids":', v_sorted_evidence,
                ',"rollback_route":', CASE WHEN NEW.rollback_route IS NOT NULL THEN to_json(trim(NEW.rollback_route)) ELSE 'null' END,
                ',"service":', CASE WHEN NEW.service IS NOT NULL THEN to_json(lower(trim(NEW.service))) ELSE 'null' END,
                '}'
            );

            -- 3. Calculate SHA-256 via pgcrypto
            v_computed_hash := encode(digest(v_canonical_json, 'sha256'), 'hex');

            IF lower(trim(NEW.plan_hash)) <> v_computed_hash THEN
                RAISE EXCEPTION 'Plan hash mismatch on insert: provided "%", recomputed "%"', NEW.plan_hash, v_computed_hash
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_action_plans_verify_plan_hash
        BEFORE INSERT ON app.action_plans
        FOR EACH ROW
        EXECUTE FUNCTION app.fn_action_plans_verify_plan_hash();
        """
    )


def downgrade() -> None:
    # 7. Drop plan hash trigger
    op.execute("DROP TRIGGER IF EXISTS trg_action_plans_verify_plan_hash ON app.action_plans;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_action_plans_verify_plan_hash();")

    # 6. Drop delete rejection triggers
    op.execute("DROP TRIGGER IF EXISTS trg_action_executions_reject_delete ON app.action_executions;")
    op.execute("DROP TRIGGER IF EXISTS trg_approvals_reject_delete ON app.approvals;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_reject_delete();")

    # 5. Drop append-only triggers
    op.execute("DROP TRIGGER IF EXISTS trg_observations_append_only ON app.observations;")
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_append_only ON app.run_events;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_reject_update_and_delete();")

    # 4. Drop plan immutability trigger
    op.execute("DROP TRIGGER IF EXISTS trg_action_plans_enforce_immutability ON app.action_plans;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_action_plans_enforce_immutability();")

    # 3. Drop runs transition trigger
    op.execute("DROP TRIGGER IF EXISTS trg_runs_enforce_transition ON app.runs;")
    op.execute("DROP FUNCTION IF EXISTS app.fn_runs_enforce_transition();")

    # 2. Drop allowed_transitions
    op.execute("DROP TABLE IF EXISTS app.allowed_transitions CASCADE;")

    # 1. Drop RLS policies
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_policy_{table} ON app.{table};")
        op.execute(f"ALTER TABLE app.{table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE app.{table} DISABLE ROW LEVEL SECURITY;")

    # 0. Revert bypassrls
    op.execute("ALTER ROLE opspilot_migrator NOBYPASSRLS;")

