"""apps/api/repositories/hypothesis_repository.py

Repository for the Hypothesis aggregate and associated hypothesis_evidence links.

Enforces:
1. TenantContext scoping on all operations (SET LOCAL app.tenant_id = :tenant_id).
2. Strict transition rules and plain-text statement validation.
3. Transactional emission of run_events:
   - hypothesis_added
   - evidence_linked
   - status_changed
   within the exact same database transaction.
4. Composite foreign-key validation: hypothesis and evidence must strictly belong
   to the same run_id and tenant_id.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from agent.graph.ledger import (
    MAX_HYPOTHESES_PER_RUN,
    VALID_CATEGORIES,
    VALID_MECHANISMS,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    validate_plain_text_statement,
)
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.models.tables import (
    HypothesisEvidenceModel,
    HypothesisModel,
    ObservationModel,
    RunModel,
)
from apps.api.repositories.base import BaseRepository
from apps.api.repositories.run_repository import RunRepository


class HypothesisRepository(BaseRepository):
    """Repository handling Hypothesis formulation, evidence linking, and status transitions."""

    def __init__(self, session: Any) -> None:
        super().__init__(session)
        self.run_repo = RunRepository(session)

    def get_by_id(self, ctx: TenantContext, run_id: UUID, hypothesis_id: UUID) -> HypothesisModel:
        """Retrieves a hypothesis by ID within the tenant and run context."""
        self.set_tenant_context(ctx)
        stmt = select(HypothesisModel).where(
            HypothesisModel.id == hypothesis_id,
            HypothesisModel.run_id == run_id,
            HypothesisModel.tenant_id == ctx.tenant_id,
        )
        hyp = self.session.scalar(stmt)
        if hyp is None:
            raise NotFoundError(
                f"Hypothesis {hypothesis_id} not found for run {run_id} and tenant {ctx.tenant_id}"
            )
        return hyp

    def list_by_run(self, ctx: TenantContext, run_id: UUID) -> list[HypothesisModel]:
        """Lists all hypotheses for a run within the tenant context."""
        self.set_tenant_context(ctx)
        stmt = (
            select(HypothesisModel)
            .where(
                HypothesisModel.run_id == run_id,
                HypothesisModel.tenant_id == ctx.tenant_id,
            )
            .order_by(HypothesisModel.created_step.asc(), HypothesisModel.created_at.asc())
        )
        return list(self.session.scalars(stmt).all())

    def add_hypothesis(
        self,
        ctx: TenantContext,
        run_id: UUID,
        statement: str,
        category: str,
        mechanism: str,
        service: str | None = None,
        confidence: float = 0.50,
        step: int = 1,
        tests_run: list[str] | None = None,
        actor: str = "investigator",
        hypothesis_id: UUID | None = None,
    ) -> HypothesisModel:
        """Persists a new hypothesis and emits hypothesis_added event in same transaction."""
        self.set_tenant_context(ctx)

        # 1. Verify run exists for this tenant
        run_stmt = select(RunModel.id).where(
            RunModel.id == run_id,
            RunModel.tenant_id == ctx.tenant_id,
        )
        if self.session.scalar(run_stmt) is None:
            raise NotFoundError(f"Run {run_id} not found for tenant {ctx.tenant_id}")

        # 2. Check maximum hypotheses capacity
        count_stmt = select(func.count(HypothesisModel.id)).where(
            HypothesisModel.run_id == run_id,
            HypothesisModel.tenant_id == ctx.tenant_id,
        )
        current_count = self.session.scalar(count_stmt) or 0
        if current_count >= MAX_HYPOTHESES_PER_RUN:
            raise ValueError(
                f"Cannot add hypothesis: run has reached maximum capacity of {MAX_HYPOTHESES_PER_RUN}"
            )

        # 3. Validate inputs
        clean_statement = validate_plain_text_statement(statement)
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{category}'; must be one of {VALID_CATEGORIES}")
        if mechanism not in VALID_MECHANISMS:
            raise ValueError(f"Invalid mechanism '{mechanism}'; must be one of {VALID_MECHANISMS}")

        hyp_id = hypothesis_id or uuid4()
        hyp = HypothesisModel(
            id=hyp_id,
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            statement=clean_statement,
            category=category,
            mechanism=mechanism,
            service=service,
            status="open",
            supporting_evidence_ids=[],
            contrary_evidence_ids=[],
            tests_run=tests_run or [],
            confidence=confidence,
            created_step=step,
            updated_step=step,
        )
        self.session.add(hyp)
        self.session.flush()

        # 4. Emit run_event in the same transaction
        self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="hypothesis_added",
            actor=actor,
            payload={
                "hypothesis_id": str(hyp.id),
                "statement": hyp.statement,
                "category": hyp.category,
                "mechanism": hyp.mechanism,
                "service": hyp.service,
                "step": step,
            },
        )

        return hyp

    def link_evidence(
        self,
        ctx: TenantContext,
        run_id: UUID,
        hypothesis_id: UUID,
        evidence_id: str,
        relation: str = "supports",
        actor: str = "investigator",
        step: int | None = None,
        tests_tool: str | None = None,
    ) -> HypothesisEvidenceModel:
        """Links an observation evidence_id to a hypothesis and emits evidence_linked event."""
        self.set_tenant_context(ctx)

        if relation not in ("supports", "contradicts"):
            raise ValueError(f"Invalid relation '{relation}'; must be 'supports' or 'contradicts'")

        # 1. Fetch hypothesis (ensures run_id & tenant_id match)
        hyp = self.get_by_id(ctx, run_id, hypothesis_id)

        # 2. Verify observation exists for this exact run and tenant
        obs_stmt = select(ObservationModel).where(
            ObservationModel.run_id == run_id,
            ObservationModel.evidence_id == evidence_id,
            ObservationModel.tenant_id == ctx.tenant_id,
        )
        obs = self.session.scalar(obs_stmt)
        if obs is None:
            raise NotFoundError(
                f"Observation '{evidence_id}' not found for run {run_id} and tenant {ctx.tenant_id}"
            )

        # 3. Create join table entry
        link = HypothesisEvidenceModel(
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            evidence_id=evidence_id,
            relation=relation,
        )
        self.session.add(link)

        # 4. Update hypothesis evidence arrays and step
        supporting = list(hyp.supporting_evidence_ids)
        contrary = list(hyp.contrary_evidence_ids)
        tests = list(hyp.tests_run)

        if relation == "supports" and evidence_id not in supporting:
            supporting.append(evidence_id)
            hyp.supporting_evidence_ids = supporting
        elif relation == "contradicts" and evidence_id not in contrary:
            contrary.append(evidence_id)
            hyp.contrary_evidence_ids = contrary

        if tests_tool and tests_tool not in tests:
            tests.append(tests_tool)
            hyp.tests_run = tests

        if step is not None:
            hyp.updated_step = max(step, hyp.updated_step or 1)

        self.session.flush()

        # 5. Emit run_event in the same transaction
        self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="evidence_linked",
            actor=actor,
            payload={
                "hypothesis_id": str(hypothesis_id),
                "evidence_id": evidence_id,
                "relation": relation,
                "step": step or hyp.updated_step,
            },
        )

        return link

    def set_status(
        self,
        ctx: TenantContext,
        run_id: UUID,
        hypothesis_id: UUID,
        new_status: str,
        actor: str = "investigator",
        step: int = 1,
    ) -> HypothesisModel:
        """Transitions validation status of a hypothesis and emits status_changed event."""
        self.set_tenant_context(ctx)

        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{new_status}'; must be one of {VALID_STATUSES}")

        hyp = self.get_by_id(ctx, run_id, hypothesis_id)
        old_status = hyp.status

        if old_status == "refuted" and new_status != "refuted":
            # Count evidence links present in join table
            link_count_stmt = select(func.count(HypothesisEvidenceModel.id)).where(
                HypothesisEvidenceModel.hypothesis_id == hypothesis_id,
                HypothesisEvidenceModel.run_id == run_id,
                HypothesisEvidenceModel.tenant_id == ctx.tenant_id,
            )
            linked_count = self.session.scalar(link_count_stmt) or 0
            if linked_count <= 0:
                raise ValueError(
                    f"Cannot reopen refuted hypothesis '{hypothesis_id}': refuted is final unless "
                    "new evidence is linked."
                )
        elif old_status != "refuted":
            allowed = VALID_TRANSITIONS.get(old_status, set())
            if new_status not in allowed:
                raise ValueError(
                    f"Invalid status transition for hypothesis '{hypothesis_id}': "
                    f"cannot transition from '{old_status}' to '{new_status}'"
                )

        hyp.status = new_status
        hyp.updated_step = max(step, hyp.updated_step or 1)
        self.session.flush()

        # Emit run_event in the same transaction
        self.run_repo.append_event(
            ctx=ctx,
            run_id=run_id,
            event_type="status_changed",
            actor=actor,
            payload={
                "hypothesis_id": str(hypothesis_id),
                "from_status": old_status,
                "to_status": new_status,
                "step": step,
            },
        )

        return hyp
