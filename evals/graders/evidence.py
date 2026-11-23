"""Deterministic evidence support grader validating groundedness, timeline window,
and tool scope.
"""

from __future__ import annotations

from datetime import datetime

from evals.graders.models import Diagnosis, EvidenceGrade, EvidenceItem, EvidenceStore

DEFAULT_ALLOWED_SOURCES: set[str] = {"prometheus", "logs", "traces", "deploys"}


def grade_evidence_support(
    diagnosis: Diagnosis,
    evidence_store: EvidenceStore | dict[str, EvidenceItem] | list[EvidenceItem],
    incident_window: tuple[datetime, datetime] | None = None,
    allowed_tools: set[str] | None = None,
) -> EvidenceGrade:
    """Verifies that all evidence items cited by a diagnosis exist, fall within the incident window,
    and originate from authorized tool scopes. Completely deterministic; no LLM.
    """
    allowed = {s.lower() for s in (allowed_tools or DEFAULT_ALLOWED_SOURCES)}

    # Normalize evidence store to a lookup dict
    lookup: dict[str, EvidenceItem] = {}
    if isinstance(evidence_store, EvidenceStore):
        lookup = evidence_store._items
    elif isinstance(evidence_store, dict):
        lookup = evidence_store
    elif isinstance(evidence_store, list):
        for entry in evidence_store:
            lookup[entry.evidence_id] = entry

    total_count = len(diagnosis.evidence_ids)
    invalid_ids: list[str] = []
    out_of_window_ids: list[str] = []
    unauthorized_tool_ids: list[str] = []
    reasons: list[str] = []

    if total_count == 0:
        return EvidenceGrade(
            supported=False,
            valid_count=0,
            total_count=0,
            invalid_ids=[],
            out_of_window_ids=[],
            unauthorized_tool_ids=[],
            reasons=["Diagnosis cites zero supporting evidence IDs"],
        )

    for ev_id in diagnosis.evidence_ids:
        # 1. Existence check
        item: EvidenceItem | None = lookup.get(ev_id)
        if item is None:
            invalid_ids.append(ev_id)
            continue

        # 2. Scope / Tool authorization check
        source_clean = item.source.lower().strip()
        if source_clean not in allowed:
            unauthorized_tool_ids.append(ev_id)

        # 3. Incident window check
        if incident_window is not None:
            window_start, window_end = incident_window
            item_ts = item.timestamp
            # Support ISO formatted strings if timestamp was parsed as string
            if isinstance(item_ts, str):
                item_ts = datetime.fromisoformat(item_ts)
            if item_ts < window_start or item_ts > window_end:
                out_of_window_ids.append(ev_id)

    # Compute valid count
    flawed_ids = set(invalid_ids + out_of_window_ids + unauthorized_tool_ids)
    valid_count = total_count - len(flawed_ids)
    is_supported = (len(flawed_ids) == 0) and (valid_count > 0)

    if is_supported:
        reasons.append(
            f"All {total_count} cited evidence IDs exist, fall within the incident window, "
            f"and use allowed tools."
        )
    else:
        if invalid_ids:
            reasons.append(
                f"Fabricated or nonexistent evidence IDs ({len(invalid_ids)}): {invalid_ids}"
            )
        if out_of_window_ids:
            reasons.append(
                f"Evidence items outside incident window ({len(out_of_window_ids)}): "
                f"{out_of_window_ids}"
            )
        if unauthorized_tool_ids:
            reasons.append(
                f"Evidence items from unauthorized scopes ({len(unauthorized_tool_ids)}): "
                f"{unauthorized_tool_ids}"
            )

    return EvidenceGrade(
        supported=is_supported,
        valid_count=valid_count,
        total_count=total_count,
        invalid_ids=invalid_ids,
        out_of_window_ids=out_of_window_ids,
        unauthorized_tool_ids=unauthorized_tool_ids,
        reasons=reasons,
    )
