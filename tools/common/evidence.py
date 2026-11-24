"""Deterministic evidence ID generation and argument normalization."""

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel

from tools.common.models import Window


def _canonicalize_value(val: Any) -> Any:
    """Recursively canonicalizes values for stable deterministic hashing."""
    if isinstance(val, datetime):
        # Normalize timezone to UTC
        if val.tzinfo is None:
            dt_utc = val.replace(tzinfo=timezone.utc)
        else:
            dt_utc = val.astimezone(timezone.utc)
        # Format as strict UTC ISO string
        if dt_utc.microsecond:
            return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, float):
        # Round floating point values to 6 decimal places to prevent float precision drift
        return round(val, 6)
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, BaseModel):
        return _canonicalize_value(val.model_dump())
    if isinstance(val, dict):
        return {str(k): _canonicalize_value(v) for k, v in sorted(val.items())}
    if isinstance(val, (list, tuple, set)):
        return [_canonicalize_value(x) for x in val]
    return val


def make_evidence_id(
    kind: str,
    normalized_args: dict[str, Any],
    window: Window,
    source_version: str | None = None,
) -> str:
    """Generate a deterministic evidence identifier.

    Format: ev_<kind>_<hash8>
    The 8-character hash is derived from SHA-256 over canonicalized arguments,
    normalized window, and source version.
    """
    canonical_payload = {
        "args": _canonicalize_value(normalized_args),
        "kind": str(kind),
        "source_version": str(source_version or ""),
        "window": {
            "start": _canonicalize_value(window.start),
            "end": _canonicalize_value(window.end),
            "step_seconds": window.step_seconds,
        },
    }

    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    hash8 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:8]
    return f"ev_{kind}_{hash8}"
