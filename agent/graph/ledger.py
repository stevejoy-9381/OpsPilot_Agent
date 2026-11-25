"""agent/graph/ledger.py

Pydantic models, ledger state containers, and pure transition functions for
the OpsPilot Hypothesis Tracking and Contrary-Evidence system (Feature 6).

Implements:
1. Frozen Pydantic models for HypothesisRecord and HypothesisLedger with strict validators:
   - statement: plain text, max 200 chars (no non-printable control chars or null bytes).
   - category and mechanism: validated against shared diagnosis enums.
   - status: open, supported, refuted, inconclusive.
   - transition rules:
       * open -> supported | refuted | inconclusive
       * supported <-> inconclusive
       * refuted is final unless new evidence is linked, which reopens it only
         through an explicit op (refuted cannot be silently reopened).
2. Pure functions (strictly no I/O):
   - add_hypothesis
   - link_evidence
   - set_status
   - leading_hypothesis (deterministic rule: highest count of valid independent-source
     supporting evidence, ties broken by earliest created step; 0% model confidence influence)
   - rivals
3. Hard cap of MAX_HYPOTHESES = 4 per run.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evals.graders.models import DiagnosisCategory, DiagnosisMechanism

# Hard cap on hypotheses per run as defined in docs/hypothesis_design.md
MAX_HYPOTHESES_PER_RUN: int = 4

HypothesisStatus = Literal["open", "supported", "refuted", "inconclusive"]
EvidenceRelation = Literal["supports", "contradicts"]

VALID_CATEGORIES: set[str] = {"bad_deploy", "dependency_failure", "false_alarm", "unknown"}
VALID_MECHANISMS: set[str] = {
    "connection_pool_exhaustion",
    "cache_bypass_latency",
    "dependency_timeout",
    "none",
    "unknown",
}

VALID_STATUSES: set[str] = {"open", "supported", "refuted", "inconclusive"}

# Allowed transitions from non-refuted states
VALID_TRANSITIONS: dict[str, set[str]] = {
    "open": {"supported", "refuted", "inconclusive", "open"},
    "supported": {"inconclusive", "refuted", "supported"},
    "inconclusive": {"supported", "refuted", "inconclusive"},
    "refuted": {"refuted"},  # Refuted cannot transition directly without new evidence
}


def validate_plain_text_statement(statement: str) -> str:
    """Validates that a statement is concise, plain-text without control characters (max 200 chars)."""
    cleaned = statement.strip()
    if not cleaned:
        raise ValueError("Hypothesis statement cannot be empty or whitespace only")
    if len(cleaned) > 200:
        raise ValueError(f"Hypothesis statement exceeds 200 characters ({len(cleaned)} chars)")
    for ch in cleaned:
        if unicodedata.category(ch).startswith("C") and ch not in ("\n", "\t"):
            raise ValueError("Hypothesis statement must be plain text without control characters")
    return cleaned


class HypothesisRecord(BaseModel):
    """Immutable audit record representing a single tracked hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str = Field(description="Unique identifier (e.g. hyp_01)")
    statement: str = Field(description="Concrete natural language proposition (max 200 chars)")
    category: DiagnosisCategory = Field(description="Standard root cause category")
    mechanism: DiagnosisMechanism = Field(description="Concrete failure mechanism")
    service: str | None = Field(default=None, description="Suspected microservice or None")
    status: HypothesisStatus = Field(default="open", description="Validation status")
    supporting_evidence_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="List of observation evidence IDs supporting this hypothesis",
    )
    contrary_evidence_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="List of observation evidence IDs refuting this hypothesis",
    )
    tests_run: tuple[Any, ...] = Field(
        default_factory=tuple,
        description="Tool queries or test descriptions dispatched against it",
    )
    confidence: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Self-reported model confidence (not used for leading choice)",
    )
    created_step: int = Field(default=1, ge=1, description="Graph step when created")
    updated_step: int = Field(default=1, ge=1, description="Graph step when last modified")
    evidence_count_at_refuted: int | None = Field(
        default=None,
        description="Total evidence items when marked refuted, used to enforce explicit reopen rule",
    )

    @property
    def id(self) -> str:
        """Alias for hypothesis_id for uniform accessor interface."""
        return self.hypothesis_id

    def to_summary_dict(self) -> dict[str, Any]:
        """Returns a compact summary dictionary suitable for LangGraph state."""
        return {
            "id": self.hypothesis_id,
            "statement": self.statement,
            "category": self.category,
            "mechanism": self.mechanism,
            "service": self.service,
            "status": self.status,
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contrary_evidence_ids": list(self.contrary_evidence_ids),
            "tests_run": list(self.tests_run),
            "confidence": round(self.confidence, 2),
            "created_step": self.created_step,
            "updated_step": self.updated_step,
        }

    @field_validator("statement")
    @classmethod
    def _validate_statement(cls, val: str) -> str:
        return validate_plain_text_statement(val)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, val: str) -> DiagnosisCategory:
        if val not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{val}'; must be one of {VALID_CATEGORIES}")
        return val  # type: ignore[return-value]

    @field_validator("mechanism")
    @classmethod
    def _validate_mechanism(cls, val: str) -> DiagnosisMechanism:
        if val not in VALID_MECHANISMS:
            raise ValueError(f"Invalid mechanism '{val}'; must be one of {VALID_MECHANISMS}")
        return val  # type: ignore[return-value]

    @field_validator("status")
    @classmethod
    def _validate_status(cls, val: str) -> HypothesisStatus:
        if val not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{val}'; must be one of {VALID_STATUSES}")
        return val  # type: ignore[return-value]


class HypothesisLedger(BaseModel):
    """Immutable collection of tracked hypotheses for an investigation run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypotheses: dict[str, HypothesisRecord] = Field(
        default_factory=dict,
        description="Map of hypothesis_id -> HypothesisRecord",
    )

    @property
    def count(self) -> int:
        return len(self.hypotheses)

    @property
    def is_full(self) -> int:
        return len(self.hypotheses) >= MAX_HYPOTHESES_PER_RUN

    def get(self, hypothesis_id: str) -> HypothesisRecord | None:
        return self.hypotheses.get(hypothesis_id)

    def __contains__(self, hypothesis_id: str) -> bool:
        return hypothesis_id in self.hypotheses


# ---------------------------------------------------------------------------
# Pure Functions (No I/O)
# ---------------------------------------------------------------------------

def extract_source_kind(evidence_id: str, evidence_sources: dict[str, str] | None = None) -> str:
    """Extracts normalized telemetry source kind from an evidence_id or lookup dict."""
    if evidence_sources and evidence_id in evidence_sources:
        src = evidence_sources[evidence_id].lower().strip().split(":")[0]
    elif evidence_id.startswith("ev_"):
        parts = evidence_id.split("_")
        src = parts[1].lower() if len(parts) > 1 else "unknown"
    else:
        src = "unknown"

    if src in ("prometheus", "metrics", "metric"):
        return "metrics"
    if src in ("logs", "log"):
        return "logs"
    if src in ("traces", "trace"):
        return "traces"
    if src in ("deploys", "deploy"):
        return "deploys"
    if src in ("diff", "deploy_diff"):
        return "diff"
    return src


def add_hypothesis(
    ledger: HypothesisLedger | dict[str, HypothesisRecord],
    statement: str,
    category: str,
    mechanism: str,
    service: str | None = None,
    confidence: float = 0.50,
    step: int = 1,
    hypothesis_id: str | None = None,
) -> tuple[HypothesisLedger, HypothesisRecord]:
    """Pure function adding a new hypothesis to the ledger.
    
    Enforces:
    - MAX_HYPOTHESES_PER_RUN cap (default 4).
    - Automatic hypothesis_id generation if not specified (hyp_01, hyp_02, ...).
    - Uniqueness of hypothesis_id.
    """
    hyp_dict = dict(ledger.hypotheses) if isinstance(ledger, HypothesisLedger) else dict(ledger)

    if len(hyp_dict) >= MAX_HYPOTHESES_PER_RUN:
        raise ValueError(
            f"Cannot add hypothesis: ledger has reached maximum capacity of {MAX_HYPOTHESES_PER_RUN}"
        )

    if hypothesis_id is None:
        idx = len(hyp_dict) + 1
        hypothesis_id = f"hyp_{idx:02d}"
        while hypothesis_id in hyp_dict:
            idx += 1
            hypothesis_id = f"hyp_{idx:02d}"

    if hypothesis_id in hyp_dict:
        raise ValueError(f"Hypothesis '{hypothesis_id}' already exists in ledger")

    new_record = HypothesisRecord(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=category,  # type: ignore[arg-type]
        mechanism=mechanism,  # type: ignore[arg-type]
        service=service,
        status="open",
        supporting_evidence_ids=(),
        contrary_evidence_ids=(),
        tests_run=(),
        confidence=confidence,
        created_step=step,
        updated_step=step,
        evidence_count_at_refuted=None,
    )

    hyp_dict[hypothesis_id] = new_record
    return HypothesisLedger(hypotheses=hyp_dict), new_record


def link_evidence(
    ledger: HypothesisLedger | dict[str, HypothesisRecord],
    hypothesis_id: str,
    evidence_id: str,
    relation: EvidenceRelation = "supports",
    tests_tool: str | None = None,
    step: int | None = None,
) -> tuple[HypothesisLedger, HypothesisRecord]:
    """Pure function linking an evidence ID to a hypothesis.
    
    If relation is 'supports', adds to supporting_evidence_ids (deduplicated).
    If relation is 'contradicts', adds to contrary_evidence_ids (deduplicated).
    Note: If the hypothesis is currently 'refuted', linking new evidence does NOT
    silently reopen it. It remains refuted, but records the new evidence, permitting
    an explicit set_status reopen call later.
    """
    hyp_dict = dict(ledger.hypotheses) if isinstance(ledger, HypothesisLedger) else dict(ledger)
    record = hyp_dict.get(hypothesis_id)
    if record is None:
        raise ValueError(f"Hypothesis '{hypothesis_id}' not found in ledger")

    supporting = list(record.supporting_evidence_ids)
    contrary = list(record.contrary_evidence_ids)
    tests = list(record.tests_run)

    if relation == "supports":
        if evidence_id not in supporting:
            supporting.append(evidence_id)
    elif relation == "contradicts":
        if evidence_id not in contrary:
            contrary.append(evidence_id)
    else:
        raise ValueError(f"Invalid relation '{relation}'; must be 'supports' or 'contradicts'")

    if tests_tool and tests_tool not in tests:
        tests.append(tests_tool)

    updated_step = step if step is not None else record.updated_step

    updated_record = record.model_copy(
        update={
            "supporting_evidence_ids": tuple(supporting),
            "contrary_evidence_ids": tuple(contrary),
            "tests_run": tuple(tests),
            "updated_step": max(updated_step, record.updated_step),
        }
    )
    hyp_dict[hypothesis_id] = updated_record
    return HypothesisLedger(hypotheses=hyp_dict), updated_record


def set_status(
    ledger: HypothesisLedger | dict[str, HypothesisRecord],
    hypothesis_id: str,
    new_status: HypothesisStatus,
    step: int | None = None,
    allow_reopen: bool = False,
) -> tuple[HypothesisLedger, HypothesisRecord]:
    """Pure function transitioning the validation status of a hypothesis.
    
    Transition rules:
    - open -> supported | refuted | inconclusive
    - supported <-> inconclusive
    - refuted cannot be transitioned directly without new evidence linked.
      If new evidence was linked after it was refuted (total evidence > evidence_count_at_refuted),
      reopening to open, supported, or inconclusive is allowed.
      Otherwise, raises ValueError.
    """
    hyp_dict = dict(ledger.hypotheses) if isinstance(ledger, HypothesisLedger) else dict(ledger)
    record = hyp_dict.get(hypothesis_id)
    if record is None:
        raise ValueError(f"Hypothesis '{hypothesis_id}' not found in ledger")

    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'; must be one of {VALID_STATUSES}")

    curr_status = record.status
    total_evidence = len(record.supporting_evidence_ids) + len(record.contrary_evidence_ids)

    # Transitioning out of refuted
    if curr_status == "refuted" and new_status != "refuted":
        # Check if new evidence was linked since it was refuted
        count_at_refuted = record.evidence_count_at_refuted
        if count_at_refuted is None or total_evidence <= count_at_refuted:
            raise ValueError(
                f"Cannot reopen refuted hypothesis '{hypothesis_id}': refuted is final unless "
                "new evidence is linked. No new evidence has been linked since it was refuted."
            )
        # Explicit op with new evidence: allowed to reopen
        evidence_count_at_refuted = None
    elif curr_status != "refuted":
        allowed = VALID_TRANSITIONS.get(curr_status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid status transition for hypothesis '{hypothesis_id}': "
                f"cannot transition from '{curr_status}' to '{new_status}'"
            )
        evidence_count_at_refuted = (
            total_evidence if new_status == "refuted" else record.evidence_count_at_refuted
        )
    else:
        # refuted -> refuted (no-op)
        evidence_count_at_refuted = record.evidence_count_at_refuted

    updated_step = step if step is not None else record.updated_step

    updated_record = record.model_copy(
        update={
            "status": new_status,
            "evidence_count_at_refuted": evidence_count_at_refuted,
            "updated_step": max(updated_step, record.updated_step),
        }
    )
    hyp_dict[hypothesis_id] = updated_record
    return HypothesisLedger(hypotheses=hyp_dict), updated_record


def leading_hypothesis(
    ledger: HypothesisLedger | dict[str, HypothesisRecord],
    evidence_sources: dict[str, str] | None = None,
) -> HypothesisRecord | None:
    """Pure function selecting the leading hypothesis deterministically.
    
    Deterministic Rule:
    1. Only non-refuted hypotheses (open, supported, inconclusive) are considered.
       Refuted hypotheses are never eligible.
    2. Ranked primarily by the count of distinct, valid independent-source kinds
       among their supporting_evidence_ids.
    3. Ties broken by earliest created_step (lower is earlier).
    4. Second tie-break by hypothesis_id (alphabetical for absolute determinism).
    5. Zero weight given to model self-assessed confidence.
    
    Returns None if ledger is empty or all hypotheses are refuted.
    """
    hyp_dict = ledger.hypotheses if isinstance(ledger, HypothesisLedger) else ledger
    candidates = [h for h in hyp_dict.values() if h.status != "refuted"]
    if not candidates:
        return None

    def _score_key(h: HypothesisRecord) -> tuple[int, int, str]:
        # Count independent source kinds
        source_kinds = {
            extract_source_kind(ev_id, evidence_sources)
            for ev_id in h.supporting_evidence_ids
        }
        # Filter out unknown if there are recognized kinds
        clean_kinds = {k for k in source_kinds if k != "unknown"}
        score = len(clean_kinds) if clean_kinds else len(source_kinds)
        # We want highest score (descending), earliest created_step (ascending),
        # alphabetical hypothesis_id (ascending)
        # Negate created_step for a descending sort, or sort with custom comparator
        return (score, -h.created_step, h.hypothesis_id)

    # The max candidate according to the deterministic rule:
    best: HypothesisRecord = candidates[0]
    best_key = _score_key(best)

    for cand in candidates[1:]:
        cand_key = _score_key(cand)
        # Compare: cand_key[0] > best_key[0] => cand wins
        if cand_key[0] > best_key[0]:
            best = cand
            best_key = cand_key
        elif cand_key[0] == best_key[0]:
            # Tie on independent source count: earliest created_step wins
            if cand.created_step < best.created_step:
                best = cand
                best_key = cand_key
            elif cand.created_step == best.created_step:
                # Tie on created_step: alphabetical hypothesis_id wins
                if cand.hypothesis_id < best.hypothesis_id:
                    best = cand
                    best_key = cand_key

    return best


def rivals(
    ledger: HypothesisLedger | dict[str, HypothesisRecord],
    evidence_sources: dict[str, str] | None = None,
    leading_id: str | None = None,
) -> list[HypothesisRecord]:
    """Pure function returning all hypotheses on the ledger competing against the leading hypothesis.
    
    If leading_id is not specified, it is determined via leading_hypothesis(ledger).
    All other hypotheses on the ledger (whether open, supported, inconclusive, or refuted)
    are returned as rivals.
    """
    hyp_dict = ledger.hypotheses if isinstance(ledger, HypothesisLedger) else ledger
    if not hyp_dict:
        return []

    if leading_id is None:
        leading = leading_hypothesis(ledger, evidence_sources=evidence_sources)
        leading_id = leading.hypothesis_id if leading else None

    if leading_id is None:
        return list(hyp_dict.values())

    return [h for h in hyp_dict.values() if h.hypothesis_id != leading_id]
