"""Canonical plan_hash computation for OpsPilot action plans.

Computes SHA-256 over canonical JSON representation:
- Keys sorted alphabetically
- Separators (",", ":") with no extra whitespace
- Datetimes converted to UTC ISO-8601 strings
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_plan_hash(
    *,
    action: str,
    service: str | None,
    expected_version: str | None,
    rationale_evidence_ids: list[str],
    rollback_route: str | None,
) -> str:
    """Computes a deterministic SHA-256 hash over canonical plan fields."""
    canonical_payload: dict[str, Any] = {
        "action": str(action).strip().lower(),
        "expected_version": expected_version.strip() if expected_version is not None else None,
        "rationale_evidence_ids": sorted(list(rationale_evidence_ids)),
        "rollback_route": rollback_route.strip() if rollback_route is not None else None,
        "service": service.strip().lower() if service is not None else None,
    }

    serialized = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
