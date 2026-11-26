"""add plan target_id, rollback_to_version, policy_version, expiry check, and one-active-plan index

Revision ID: 95e6f7a8b9c0
Revises: 84d5e6f7a8b9
Create Date: 2026-11-25 20:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "84d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("action_plans", sa.Column("target_id", sa.String(length=64), nullable=True), schema="app")
    op.add_column(
        "action_plans",
        sa.Column("rollback_to_version", sa.String(length=64), nullable=True),
        schema="app",
    )
    op.add_column("action_plans", sa.Column("policy_version", sa.String(length=64), nullable=True), schema="app")
    op.alter_column(
        "action_plans",
        "rollback_route",
        existing_type=sa.String(length=128),
        type_=sa.String(length=255),
        schema="app",
    )

    op.execute(
        """
        CREATE UNIQUE INDEX uq_action_plans_one_proposed_per_run
        ON app.action_plans (run_id)
        WHERE status = 'proposed';
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_plans_enforce_immutability()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.action <> OLD.action OR
               NEW.service IS DISTINCT FROM OLD.service OR
               NEW.target_id IS DISTINCT FROM OLD.target_id OR
               NEW.expected_version IS DISTINCT FROM OLD.expected_version OR
               NEW.rollback_to_version IS DISTINCT FROM OLD.rollback_to_version OR
               NEW.rationale_evidence_ids <> OLD.rationale_evidence_ids OR
               NEW.rollback_route IS DISTINCT FROM OLD.rollback_route OR
               NEW.plan_hash <> OLD.plan_hash OR
               NEW.policy_version IS DISTINCT FROM OLD.policy_version OR
               NEW.run_id <> OLD.run_id OR
               NEW.tenant_id <> OLD.tenant_id THEN
                RAISE EXCEPTION 'Immutable plan fields (action, service, target_id, expected_version, rollback_to_version, rationale_evidence_ids, rollback_route, plan_hash, policy_version, run_id, tenant_id) cannot be modified'
                    USING ERRCODE = 'check_violation';
            END IF;

            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fn_action_plans_verify_plan_hash()
        RETURNS TRIGGER AS $$
        DECLARE
            v_canonical_json TEXT;
            v_computed_hash TEXT;
            v_sorted_evidence TEXT;
        BEGIN
            IF NEW.status = 'proposed' AND NEW.expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'expires_at must be in the future at insert'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.target_id IS NOT NULL THEN
                SELECT COALESCE('[' || string_agg(to_json(elem)::text, ',' ORDER BY elem) || ']', '[]')
                INTO v_sorted_evidence
                FROM (SELECT DISTINCT elem FROM unnest(NEW.rationale_evidence_ids) AS elem) d;

                v_canonical_json := concat(
                    '{"action":', to_json(lower(trim(NEW.action))),
                    ',"expected_version":', to_json(trim(NEW.expected_version)),
                    ',"expires_at":', to_json(to_char((NEW.expires_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
                    ',"rationale_evidence_ids":', COALESCE(v_sorted_evidence, '[]'),
                    ',"rollback_to_version":', CASE WHEN NEW.rollback_to_version IS NOT NULL THEN to_json(trim(NEW.rollback_to_version)) ELSE 'null' END,
                    ',"run_id":', to_json(trim(NEW.run_id::text)),
                    ',"service":', to_json(lower(trim(NEW.service))),
                    ',"target_id":', to_json(trim(NEW.target_id)),
                    ',"tenant_id":', to_json(trim(NEW.tenant_id::text)),
                    '}'
                );
            ELSE
                SELECT COALESCE('[' || string_agg(to_json(elem)::text, ',' ORDER BY elem) || ']', '[]')
                INTO v_sorted_evidence
                FROM unnest(NEW.rationale_evidence_ids) AS elem;

                v_canonical_json := concat(
                    '{"action":', to_json(lower(trim(NEW.action))),
                    ',"expected_version":', CASE WHEN NEW.expected_version IS NOT NULL THEN to_json(trim(NEW.expected_version)) ELSE 'null' END,
                    ',"rationale_evidence_ids":', COALESCE(v_sorted_evidence, '[]'),
                    ',"rollback_route":', CASE WHEN NEW.rollback_route IS NOT NULL THEN to_json(trim(NEW.rollback_route)) ELSE 'null' END,
                    ',"service":', CASE WHEN NEW.service IS NOT NULL THEN to_json(lower(trim(NEW.service))) ELSE 'null' END,
                    '}'
                );
            END IF;

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
    op.execute("DROP INDEX IF EXISTS app.uq_action_plans_one_proposed_per_run;")
    op.drop_column("action_plans", "policy_version", schema="app")
    op.drop_column("action_plans", "rollback_to_version", schema="app")
    op.drop_column("action_plans", "target_id", schema="app")
