"""Canonical plan_hash computation for OpsPilot action plans.

Delegates canonical JSON serialization and SHA-256 hashing to shared.plans
so there is a single canonical implementation across the codebase.
"""

from __future__ import annotations

from typing import Any

from shared.plans import sha256_canonical_json


def compute_plan_hash(
    *,
    action: str,
    service: str | None,
    expected_version: str | None,
    rationale_evidence_ids: list[str],
    rollback_route: str | None,
) -> str:
    """Computes a deterministic SHA-256 hash over legacy Feature 4.3 canonical plan fields."""
    canonical_payload: dict[str, Any] = {
        "action": str(action).strip().lower(),
        "expected_version": expected_version.strip() if expected_version is not None else None,
        "rationale_evidence_ids": sorted(list(rationale_evidence_ids)),
        "rollback_route": rollback_route.strip() if rollback_route is not None else None,
        "service": service.strip().lower() if service is not None else None,
    }
    return sha256_canonical_json(canonical_payload)
