"""extend hypotheses and add hypothesis_evidence

Revision ID: 62b3c4d5e6f7
Revises: 51a2b3c4d5e6
Create Date: 2026-11-25 12:30:00.000000

Adds to app.hypotheses:
  - tests_run JSONB NOT NULL DEFAULT '[]'
  - created_step INTEGER NOT NULL DEFAULT 1 CHECK (created_step >= 1)
  - updated_step INTEGER NOT NULL DEFAULT 1 CHECK (updated_step >= 1)
  - updates status check constraint to include 'inconclusive'
  - adds UNIQUE (run_id, id) constraint on app.hypotheses

Creates app.hypothesis_evidence:
  - id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  - tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT
  - run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE CASCADE
  - hypothesis_id UUID NOT NULL
  - evidence_id VARCHAR(64) NOT NULL
  - relation VARCHAR(32) NOT NULL CHECK (relation IN ('supports', 'contradicts'))
  - created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
  - UNIQUE (hypothesis_id, evidence_id, relation)
  - Composite FKs tying hypothesis and observation strictly to the same run:
      FOREIGN KEY (run_id, hypothesis_id) REFERENCES app.hypotheses(run_id, id)
      FOREIGN KEY (run_id, evidence_id) REFERENCES app.observations(run_id, evidence_id)
  - Enables and forces Row-Level Security with tenant_isolation_policy
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "62b3c4d5e6f7"
down_revision: Union[str, None] = "51a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Extend app.hypotheses with new fields and composite unique constraint
    op.execute(
        """
        ALTER TABLE app.hypotheses
        ADD COLUMN IF NOT EXISTS tests_run JSONB NOT NULL DEFAULT '[]',
        ADD COLUMN IF NOT EXISTS created_step INTEGER NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS updated_step INTEGER NOT NULL DEFAULT 1;

        -- Add step validation constraints if not present
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_hypotheses_created_step'
            ) THEN
                ALTER TABLE app.hypotheses
                ADD CONSTRAINT chk_hypotheses_created_step CHECK (created_step >= 1);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_hypotheses_updated_step'
            ) THEN
                ALTER TABLE app.hypotheses
                ADD CONSTRAINT chk_hypotheses_updated_step CHECK (updated_step >= 1);
            END IF;
        END $$;

        -- Update status check constraint to include 'inconclusive'
        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS hypotheses_status_check;
        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS chk_hypotheses_status;
        ALTER TABLE app.hypotheses ADD CONSTRAINT chk_hypotheses_status
            CHECK (status IN ('open', 'supported', 'refuted', 'inconclusive'));

        -- Ensure unique constraint on (run_id, id) for composite FK references
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_hypotheses_run_id'
            ) THEN
                ALTER TABLE app.hypotheses ADD CONSTRAINT uq_hypotheses_run_id UNIQUE (run_id, id);
            END IF;
        END $$;
        """
    )

    # 2. Create join table app.hypothesis_evidence with composite FKs tying evidence to same run
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.hypothesis_evidence (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
            run_id UUID NOT NULL REFERENCES app.runs(id) ON DELETE CASCADE,
            hypothesis_id UUID NOT NULL,
            evidence_id VARCHAR(64) NOT NULL,
            relation VARCHAR(32) NOT NULL CHECK (relation IN ('supports', 'contradicts')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT uq_hypothesis_evidence_triple UNIQUE (hypothesis_id, evidence_id, relation),
            CONSTRAINT fk_hypothesis_evidence_hypothesis FOREIGN KEY (run_id, hypothesis_id)
                REFERENCES app.hypotheses(run_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_hypothesis_evidence_observation FOREIGN KEY (run_id, evidence_id)
                REFERENCES app.observations(run_id, evidence_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_lookup
            ON app.hypothesis_evidence (hypothesis_id, relation);
        CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_run
            ON app.hypothesis_evidence (run_id);

        -- 3. Row-Level Security for app.hypothesis_evidence
        ALTER TABLE app.hypothesis_evidence ENABLE ROW LEVEL SECURITY;
        ALTER TABLE app.hypothesis_evidence FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS tenant_isolation_policy_hypothesis_evidence ON app.hypothesis_evidence;
        CREATE POLICY tenant_isolation_policy_hypothesis_evidence ON app.hypothesis_evidence
            FOR ALL
            USING (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            );

        -- 4. Grant DML to app roles
        GRANT SELECT, INSERT, UPDATE, DELETE ON app.hypothesis_evidence TO opspilot_api, opspilot_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        -- 1. Drop app.hypothesis_evidence
        DROP POLICY IF EXISTS tenant_isolation_policy_hypothesis_evidence ON app.hypothesis_evidence;
        DROP TABLE IF EXISTS app.hypothesis_evidence;

        -- 2. Revert app.hypotheses changes
        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS uq_hypotheses_run_id;
        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS chk_hypotheses_status;
        ALTER TABLE app.hypotheses ADD CONSTRAINT hypotheses_status_check
            CHECK (status IN ('open', 'supported', 'refuted'));

        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS chk_hypotheses_updated_step;
        ALTER TABLE app.hypotheses DROP CONSTRAINT IF EXISTS chk_hypotheses_created_step;
        ALTER TABLE app.hypotheses DROP COLUMN IF EXISTS updated_step;
        ALTER TABLE app.hypotheses DROP COLUMN IF EXISTS created_step;
        ALTER TABLE app.hypotheses DROP COLUMN IF EXISTS tests_run;
        """
    )
