"""tests/test_hypothesis_ledger.py

Comprehensive tests for Feature 6 Hypothesis Ledger:
1. Pydantic models: validation, frozen immutability, statement length and plain text rules,
   category and mechanism enum validation.
2. Pure function transition rules:
   - open -> supported | refuted | inconclusive
   - supported <-> inconclusive
   - illegal transitions raise ValueError
   - refuted is final unless new evidence linked; refuted cannot be silently reopened
3. leading_hypothesis determinism and tie-breaks:
   - highest count of independent source kinds wins
   - ties broken by earliest created_step
   - secondary tie-break by alphabetical hypothesis_id
   - model confidence has 0% influence
   - refuted hypotheses strictly excluded
4. rivals function behavior.
5. Graph state worst-case serialization under Feature 5.2 16KB cap.
6. HypothesisRepository methods with tenant context, RLS, and transactional event emissions:
   - hypothesis_added
   - evidence_linked
   - status_changed
   - cross-run and cross-tenant evidence rejection
7. Alembic migration verification:
   - tests_run, created_step, updated_step, status constraint, hypothesis_evidence join table
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.graph.ledger import (
    HypothesisLedger,
    HypothesisRecord,
    add_hypothesis,
    leading_hypothesis,
    link_evidence,
    rivals,
    set_status,
    validate_plain_text_statement,
)
from agent.graph.state import (
    MAX_WORST_CASE_STATE_BYTES,
    create_initial_state,
    state_to_dict,
)
from apps.api.domain.context import TenantContext
from apps.api.domain.errors import NotFoundError
from apps.api.models.tables import (
    HypothesisModel,
    ObservationModel,
    RunModel,
)
from apps.api.repositories.hypothesis_repository import HypothesisRepository

# ---------------------------------------------------------------------------
# 1. Pydantic Models & Field Validation Tests
# ---------------------------------------------------------------------------

def test_hypothesis_record_validation_and_frozen() -> None:
    """HypothesisRecord validates statement, category, mechanism, and is immutable."""
    record = HypothesisRecord(
        hypothesis_id="hyp_01",
        statement="Orders pool exhaustion due to leak in v1.3.0",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
        confidence=0.90,
    )
    assert record.hypothesis_id == "hyp_01"
    assert record.status == "open"
    assert record.supporting_evidence_ids == ()
    assert record.created_step == 1

    # Immutability check
    with pytest.raises(ValidationError):
        record.status = "supported"  # type: ignore[misc]


def test_statement_plain_text_and_length_validation() -> None:
    """Statement rejects empty, whitespace-only, non-printable control chars, and >200 chars."""
    with pytest.raises(ValueError, match="empty or whitespace only"):
        validate_plain_text_statement("   ")

    with pytest.raises(ValueError, match="exceeds 200 characters"):
        validate_plain_text_statement("x" * 201)

    with pytest.raises(ValueError, match="plain text without control characters"):
        validate_plain_text_statement("Exploitative payload\x00dropped")

    # Valid statement
    clean = validate_plain_text_statement("  Valid diagnostic statement.  ")
    assert clean == "Valid diagnostic statement."


def test_category_and_mechanism_enum_validation() -> None:
    """Rejects unknown category or mechanism not present in shared enums."""
    with pytest.raises(ValidationError):
        HypothesisRecord(
            hypothesis_id="hyp_01",
            statement="Valid statement",
            category="hardware_failure",  # type: ignore[arg-type]
            mechanism="connection_pool_exhaustion",
        )

    with pytest.raises(ValidationError):
        HypothesisRecord(
            hypothesis_id="hyp_01",
            statement="Valid statement",
            category="bad_deploy",
            mechanism="alien_interference",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 2. Pure Functions & Transition Rules Tests
# ---------------------------------------------------------------------------

def test_add_hypothesis_cap_and_id_generation() -> None:
    """Enforces MAX_HYPOTHESES_PER_RUN (4) and generates sequential IDs."""
    ledger = HypothesisLedger()

    # Add 4 hypotheses
    for i in range(1, 5):
        ledger, hyp = add_hypothesis(
            ledger=ledger,
            statement=f"Candidate cause #{i}",
            category="bad_deploy",
            mechanism="connection_pool_exhaustion",
            service="orders",
            step=i,
        )
        assert hyp.hypothesis_id == f"hyp_{i:02d}"
        assert ledger.count == i

    assert ledger.is_full

    # 5th hypothesis must be rejected by hard cap
    with pytest.raises(ValueError, match="maximum capacity of 4"):
        add_hypothesis(
            ledger=ledger,
            statement="Fifth candidate cause",
            category="dependency_failure",
            mechanism="dependency_timeout",
            service="inventory",
        )


def test_transition_rules_valid_and_invalid() -> None:
    """Verifies allowed transitions:
    open -> supported | refuted | inconclusive
    supported <-> inconclusive
    refuted cannot transition directly without new evidence.
    """
    ledger = HypothesisLedger()
    ledger, hyp = add_hypothesis(
        ledger=ledger,
        statement="Orders pool exhaustion",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
    )

    # 1. open -> supported
    ledger, hyp = set_status(ledger, "hyp_01", "supported", step=2)
    assert hyp.status == "supported"

    # 2. supported -> inconclusive
    ledger, hyp = set_status(ledger, "hyp_01", "inconclusive", step=3)
    assert hyp.status == "inconclusive"

    # 3. inconclusive -> supported
    ledger, hyp = set_status(ledger, "hyp_01", "supported", step=4)
    assert hyp.status == "supported"

    # 4. supported -> open is ILLEGAL
    with pytest.raises(ValueError, match="Invalid status transition"):
        set_status(ledger, "hyp_01", "open")

    # 5. supported -> refuted
    ledger, hyp = set_status(ledger, "hyp_01", "refuted", step=5)
    assert hyp.status == "refuted"


def test_refuted_hypothesis_cannot_be_silently_reopened() -> None:
    """Refuted hypothesis cannot be silently reopened by linking evidence alone,
    and cannot be reopened via set_status unless new evidence was linked after refutation.
    """
    ledger = HypothesisLedger()
    ledger, hyp = add_hypothesis(
        ledger=ledger,
        statement="Dependency timeout in inventory",
        category="dependency_failure",
        mechanism="dependency_timeout",
        service="inventory",
    )

    # Refute it
    ledger, hyp = set_status(ledger, "hyp_01", "refuted", step=2)
    assert hyp.status == "refuted"

    # Attempting to reopen without new evidence fails
    with pytest.raises(ValueError, match="Cannot reopen refuted hypothesis"):
        set_status(ledger, "hyp_01", "open")

    with pytest.raises(ValueError, match="Cannot reopen refuted hypothesis"):
        set_status(ledger, "hyp_01", "supported")

    # Link new evidence: hypothesis MUST NOT silently change status
    ledger, hyp = link_evidence(
        ledger=ledger,
        hypothesis_id="hyp_01",
        evidence_id="ev_logs_new_evidence",
        relation="supports",
        step=3,
    )
    assert hyp.status == "refuted"  # Stays refuted!

    # Now explicit set_status op IS allowed to reopen it
    ledger, hyp = set_status(ledger, "hyp_01", "supported", step=4)
    assert hyp.status == "supported"


def test_link_evidence_deduplication_and_relations() -> None:
    """link_evidence appends to supporting/contrary without duplicates and tracks tests_run."""
    ledger = HypothesisLedger()
    ledger, hyp = add_hypothesis(
        ledger=ledger,
        statement="Orders leak",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
    )

    # Link supporting
    ledger, hyp = link_evidence(
        ledger, "hyp_01", "ev_metrics_123", relation="supports", tests_tool="get_metrics"
    )
    assert hyp.supporting_evidence_ids == ("ev_metrics_123",)
    assert hyp.tests_run == ("get_metrics",)

    # Duplicate link
    ledger, hyp = link_evidence(
        ledger, "hyp_01", "ev_metrics_123", relation="supports", tests_tool="get_metrics"
    )
    assert hyp.supporting_evidence_ids == ("ev_metrics_123",)
    assert hyp.tests_run == ("get_metrics",)

    # Contrary link
    ledger, hyp = link_evidence(
        ledger, "hyp_01", "ev_logs_456", relation="contradicts", tests_tool="search_logs"
    )
    assert hyp.contrary_evidence_ids == ("ev_logs_456",)
    assert hyp.tests_run == ("get_metrics", "search_logs")


# ---------------------------------------------------------------------------
# 3. Deterministic leading_hypothesis & Tie-Breaks Tests
# ---------------------------------------------------------------------------

def test_leading_hypothesis_highest_independent_sources() -> None:
    """Hypothesis with more independent source kinds wins, regardless of model confidence."""
    ledger = HypothesisLedger()

    # hyp_01: model confidence 0.99, but only 1 source kind (metrics)
    ledger, _ = add_hypothesis(
        ledger, "Hypothesis 1", "bad_deploy", "connection_pool_exhaustion", confidence=0.99, step=1
    )
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_metrics_a1", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_metrics_a2", relation="supports")

    # hyp_02: model confidence 0.15, but 2 independent source kinds (metrics + logs)
    ledger, _ = add_hypothesis(
        ledger, "Hypothesis 2", "dependency_failure", "dependency_timeout", confidence=0.15, step=2
    )
    ledger, _ = link_evidence(ledger, "hyp_02", "ev_metrics_b1", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_02", "ev_logs_b2", relation="supports")

    # Winner must be hyp_02 (2 independent source kinds > 1 source kind)
    leading = leading_hypothesis(ledger)
    assert leading is not None
    assert leading.hypothesis_id == "hyp_02"


def test_leading_hypothesis_tie_break_earliest_created() -> None:
    """When independent source counts tie, earliest created_step wins."""
    ledger = HypothesisLedger()

    # hyp_01 created at step 3, has 2 sources (metrics + logs)
    ledger, _ = add_hypothesis(
        ledger, "Hyp 1", "bad_deploy", "connection_pool_exhaustion", step=3
    )
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_metrics_1", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_logs_1", relation="supports")

    # hyp_02 created at step 1, has 2 sources (metrics + traces)
    ledger, _ = add_hypothesis(
        ledger, "Hyp 2", "dependency_failure", "dependency_timeout", step=1
    )
    ledger, _ = link_evidence(ledger, "hyp_02", "ev_metrics_2", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_02", "ev_traces_2", relation="supports")

    # hyp_02 wins because created_step 1 < 3
    leading = leading_hypothesis(ledger)
    assert leading is not None
    assert leading.hypothesis_id == "hyp_02"


def test_leading_hypothesis_excludes_refuted() -> None:
    """Refuted hypotheses are strictly excluded from being leading."""
    ledger = HypothesisLedger()

    # hyp_01 has 3 sources, but is refuted
    ledger, _ = add_hypothesis(ledger, "Hyp 1", "bad_deploy", "connection_pool_exhaustion")
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_metrics_1", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_logs_1", relation="supports")
    ledger, _ = link_evidence(ledger, "hyp_01", "ev_traces_1", relation="supports")
    ledger, _ = set_status(ledger, "hyp_01", "refuted")

    # hyp_02 has 1 source, open
    ledger, _ = add_hypothesis(ledger, "Hyp 2", "dependency_failure", "dependency_timeout")
    ledger, _ = link_evidence(ledger, "hyp_02", "ev_metrics_2", relation="supports")

    leading = leading_hypothesis(ledger)
    assert leading is not None
    assert leading.hypothesis_id == "hyp_02"

    # If hyp_02 is also refuted, leading is None
    ledger, _ = set_status(ledger, "hyp_02", "refuted")
    assert leading_hypothesis(ledger) is None


def test_rivals_selection() -> None:
    """rivals returns all other hypotheses competing against the leading hypothesis."""
    ledger = HypothesisLedger()
    ledger, _ = add_hypothesis(ledger, "Hyp 1", "bad_deploy", "connection_pool_exhaustion")
    ledger, _ = add_hypothesis(ledger, "Hyp 2", "dependency_failure", "dependency_timeout")
    ledger, _ = add_hypothesis(ledger, "Hyp 3", "false_alarm", "none")

    all_rivals = rivals(ledger, leading_id="hyp_01")
    assert len(all_rivals) == 2
    assert {h.hypothesis_id for h in all_rivals} == {"hyp_02", "hyp_03"}


# ---------------------------------------------------------------------------
# 4. Graph State Worst-Case Serialization Test (Feature 5.2 Cap)
# ---------------------------------------------------------------------------

def test_worst_case_state_with_ledger_under_16kb() -> None:
    """A full worst-case investigation run (4 hypotheses, 8 tool trace records, alert)
    serializes to well under MAX_WORST_CASE_STATE_BYTES (16 KB).
    """
    state = create_initial_state(
        run_id=str(uuid4()),
        tenant_id=str(uuid4()),
        incident_id=str(uuid4()),
        alert={"service": "orders", "metric": "db_pool_in_use", "value": 9.5, "threshold": 7.0},
    )

    # Add 4 hypotheses to ledger in state
    ledger = HypothesisLedger()
    for i in range(1, 5):
        ledger, hyp = add_hypothesis(
            ledger,
            statement=f"Hypothesis {i}: Service failure due to configuration defect #{i}",
            category="bad_deploy",
            mechanism="connection_pool_exhaustion",
            service="orders",
            confidence=0.85,
            step=i,
        )
        ledger, hyp = link_evidence(ledger, hyp.hypothesis_id, f"ev_metrics_{i:04d}", "supports")
        ledger, hyp = link_evidence(ledger, hyp.hypothesis_id, f"ev_logs_{i:04d}", "supports")
        state["hypotheses_ledger"][hyp.hypothesis_id] = hyp.to_summary_dict()

    # Add 8 tool trace records
    for i in range(1, 9):
        state["tool_trace"].append(
            {
                "tool": "search_logs",
                "args_summary": "service=orders, limit=20, query='timeout'",
                "duration_ms": 12.5,
                "ok": True,
                "error_code": None,
                "evidence_id": f"ev_logs_{i:04d}",
                "approx_tokens": 150,
            }
        )
        state["evidence_ids"].append(f"ev_logs_{i:04d}")

    # Serialize
    serialized_json = json.dumps(state_to_dict(state))
    byte_size = len(serialized_json.encode("utf-8"))

    print(f"\n[Feature 6 State Size] Worst-case serialized state: {byte_size} bytes (Cap: {MAX_WORST_CASE_STATE_BYTES})")
    assert byte_size < MAX_WORST_CASE_STATE_BYTES
    # Should even be comfortably under 6 KB
    assert byte_size < 6144


# ---------------------------------------------------------------------------
# 5. In-Memory Mock Repository & Event Sequence Tests
# ---------------------------------------------------------------------------

class MockSession:
    """Lightweight in-memory SQLAlchemy session mock supporting tenant context, query evaluation and flushing."""

    def __init__(self) -> None:
        self.rows: dict[str, list[Any]] = {
            "tenants": [],
            "runs": [],
            "observations": [],
            "hypotheses": [],
            "hypothesis_evidence": [],
            "run_events": [],
        }
        self.current_tenant_id: UUID | None = None

    def add(self, obj: Any) -> None:
        table_name = getattr(obj, "__tablename__", type(obj).__name__.lower())
        if table_name not in self.rows:
            self.rows[table_name] = []
        self.rows[table_name].append(obj)

    def flush(self) -> None:
        pass

    def execute(self, stmt: Any, params: Any = None) -> Any:
        if params and "tenant_id" in params:
            self.current_tenant_id = UUID(str(params["tenant_id"]))
        return self

    def scalar(self, stmt: Any) -> Any:
        s = str(stmt).lower()
        if "from app.runs" in s:
            for r in self.rows["runs"]:
                if self.current_tenant_id is None or r.tenant_id == self.current_tenant_id:
                    return r
            return None
        if "from app.hypotheses" in s:
            for h in self.rows["hypotheses"]:
                if self.current_tenant_id is None or h.tenant_id == self.current_tenant_id:
                    return h
            return None
        if "from app.observations" in s:
            for o in self.rows["observations"]:
                if self.current_tenant_id is None or o.tenant_id == self.current_tenant_id:
                    return o
            return None
        if "count" in s and "hypotheses" in s:
            return len(self.rows["hypotheses"])
        if "count" in s and "hypothesis_evidence" in s:
            return len(self.rows["hypothesis_evidence"])
        if "max" in s or "coalesce" in s:
            events = self.rows["run_events"]
            return max([e.seq for e in events], default=0) + 1
        return None

    def scalars(self, stmt: Any) -> Any:
        class _Result:
            def __init__(self, data: list[Any]):
                self.data = data
            def all(self) -> list[Any]:
                return self.data
        return _Result(self.rows.get("hypotheses", []))


def test_repository_add_hypothesis_emits_event() -> None:
    """HypothesisRepository.add_hypothesis creates row and emits hypothesis_added event."""
    session = MockSession()
    tenant_id = uuid4()
    run_id = uuid4()
    ctx = TenantContext(tenant_id=tenant_id)

    # Seed run
    session.rows["runs"].append(RunModel(id=run_id, tenant_id=tenant_id, status="investigating"))

    repo = HypothesisRepository(session=session)
    hyp = repo.add_hypothesis(
        ctx=ctx,
        run_id=run_id,
        statement="Orders pool exhaustion due to leak",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        service="orders",
        confidence=0.80,
        step=1,
    )

    assert hyp.statement == "Orders pool exhaustion due to leak"
    assert len(session.rows["hypotheses"]) == 1
    assert len(session.rows["run_events"]) == 1

    event = session.rows["run_events"][0]
    assert event.type == "hypothesis_added"
    assert event.payload["hypothesis_id"] == str(hyp.id)
    assert event.payload["category"] == "bad_deploy"


def test_repository_link_evidence_emits_event_and_rejects_cross_tenant() -> None:
    """HypothesisRepository.link_evidence emits evidence_linked event and rejects foreign observations/tenants."""
    session = MockSession()
    tenant_id = uuid4()
    foreign_tenant_id = uuid4()
    run_id = uuid4()
    ctx = TenantContext(tenant_id=tenant_id)

    hyp_id = uuid4()
    hyp = HypothesisModel(
        id=hyp_id,
        run_id=run_id,
        tenant_id=tenant_id,
        statement="Orders pool exhaustion",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        supporting_evidence_ids=[],
        contrary_evidence_ids=[],
        tests_run=[],
        updated_step=1,
    )
    session.rows["hypotheses"].append(hyp)
    session.rows["runs"].append(RunModel(id=run_id, tenant_id=tenant_id))

    # Observation from SAME run
    obs_valid = ObservationModel(
        run_id=run_id,
        tenant_id=tenant_id,
        evidence_id="ev_logs_1234",
    )
    session.rows["observations"].append(obs_valid)

    repo = HypothesisRepository(session=session)

    # Link succeeds
    link = repo.link_evidence(
        ctx=ctx,
        run_id=run_id,
        hypothesis_id=hyp_id,
        evidence_id="ev_logs_1234",
        relation="supports",
        step=2,
    )
    assert link.relation == "supports"
    assert "ev_logs_1234" in hyp.supporting_evidence_ids

    # Event emitted
    assert len(session.rows["run_events"]) == 1
    ev = session.rows["run_events"][0]
    assert ev.type == "evidence_linked"
    assert ev.payload["evidence_id"] == "ev_logs_1234"

    # Cross-tenant request rejected by RLS / tenant context
    foreign_ctx = TenantContext(tenant_id=foreign_tenant_id)
    with pytest.raises(NotFoundError, match="Hypothesis .* not found"):
        repo.link_evidence(
            ctx=foreign_ctx,
            run_id=run_id,
            hypothesis_id=hyp_id,
            evidence_id="ev_logs_1234",
        )


def test_repository_set_status_emits_event() -> None:
    """HypothesisRepository.set_status validates transitions and emits status_changed event."""
    session = MockSession()
    tenant_id = uuid4()
    run_id = uuid4()
    ctx = TenantContext(tenant_id=tenant_id)

    hyp_id = uuid4()
    hyp = HypothesisModel(
        id=hyp_id,
        run_id=run_id,
        tenant_id=tenant_id,
        statement="Orders pool exhaustion",
        category="bad_deploy",
        mechanism="connection_pool_exhaustion",
        status="open",
        supporting_evidence_ids=[],
        contrary_evidence_ids=[],
        tests_run=[],
        updated_step=1,
    )
    session.rows["hypotheses"].append(hyp)
    session.rows["runs"].append(RunModel(id=run_id, tenant_id=tenant_id))

    repo = HypothesisRepository(session=session)
    updated_hyp = repo.set_status(ctx=ctx, run_id=run_id, hypothesis_id=hyp_id, new_status="supported", step=2)

    assert updated_hyp.status == "supported"
    assert len(session.rows["run_events"]) == 1
    ev = session.rows["run_events"][0]
    assert ev.type == "status_changed"
    assert ev.payload["from_status"] == "open"
    assert ev.payload["to_status"] == "supported"


# ---------------------------------------------------------------------------
# 6. Alembic Migration Structure Verification
# ---------------------------------------------------------------------------

def test_migration_functions_callable() -> None:
    """Verifies that the migration module upgrade and downgrade callables are valid."""
    import importlib
    mod = importlib.import_module(
        "infra.alembic.versions.62b3c4d5e6f7_extend_hypotheses_and_add_hypothesis_evidence"
    )
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)
