"""Evidence storage protocol and implementations.

Features:
- Protocol `EvidenceStore`:
    put(record: EvidenceRecord) -> None
    get(evidence_id, run_id, tenant_id=None) -> EvidenceRecord | None
    list_for_run(run_id, tenant_id=None) -> list[EvidenceRecord]
    exists(evidence_id, run_id, tenant_id=None) -> bool
- `EvidenceRecord`: Complete audit metadata with deterministic content_hash.
- `InMemoryEvidenceStore`: In-memory store for unit tests.
- `JsonlEvidenceStore`: Tenant-scoped append-only JSONL files per run.
- Guardrails: Refuses cross-tenant and cross-run reads.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


def compute_content_hash(payload: Any) -> str:
    """Compute deterministic SHA-256 hash of evidence payload."""
    serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class EvidenceRecord(BaseModel):
    """Immutable audit record for an item of captured evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    run_id: str
    tenant_id: str
    tool: str
    source_id: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    window_start: datetime
    window_end: datetime
    args: dict[str, Any]
    summary: str
    payload: Any = None
    payload_ref: str | None = None
    source_version: str = "v1"
    content_hash: str = Field(default="")

    def model_post_init(self, __context: Any) -> None:
        if not self.content_hash:
            computed = compute_content_hash(self.payload if self.payload is not None else self.payload_ref)
            object.__setattr__(self, "content_hash", computed)


@runtime_checkable
class EvidenceStore(Protocol):
    """Protocol for evidence persistence."""

    def put(self, record: EvidenceRecord) -> None:
        """Store an evidence record. Must be idempotent by evidence_id."""
        ...

    def get(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        """Retrieve an evidence record. Must refuse cross-run and cross-tenant access."""
        ...

    def list_for_run(self, run_id: str, tenant_id: str | None = None) -> list[EvidenceRecord]:
        """List all evidence records for a run, scoped to tenant if provided."""
        ...

    def exists(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> bool:
        """Check if an evidence record exists for a run and tenant."""
        ...


class InMemoryEvidenceStore:
    """In-memory evidence store for testing."""

    def __init__(self) -> None:
        # Key: (run_id, evidence_id)
        self._records: dict[tuple[str, str], EvidenceRecord] = {}

    def put(self, record: EvidenceRecord) -> None:
        key = (record.run_id, record.evidence_id)
        if key not in self._records:
            self._records[key] = record

    def get(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        key = (run_id, evidence_id)
        record = self._records.get(key)
        if record is None:
            return None
        if tenant_id is not None and record.tenant_id != tenant_id:
            return None
        return record

    def list_for_run(self, run_id: str, tenant_id: str | None = None) -> list[EvidenceRecord]:
        results: list[EvidenceRecord] = []
        for (r_id, _), record in self._records.items():
            if r_id == run_id:
                if tenant_id is None or record.tenant_id == tenant_id:
                    results.append(record)
        return results

    def exists(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> bool:
        return self.get(evidence_id, run_id, tenant_id) is not None


class JsonlEvidenceStore:
    """Filesystem append-only JSONL store partitioned by tenant and run."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def _get_run_file(self, tenant_id: str, run_id: str) -> Path:
        # Path: base_dir / tenant_id / f"{run_id}.jsonl"
        return self.base_dir / tenant_id / f"{run_id}.jsonl"

    def put(self, record: EvidenceRecord) -> None:
        if self.exists(record.evidence_id, record.run_id, record.tenant_id):
            return

        run_file = self._get_run_file(record.tenant_id, record.run_id)
        run_file.parent.mkdir(parents=True, exist_ok=True)

        line = record.model_dump_json() + "\n"
        with open(run_file, "a", encoding="utf-8") as f:
            f.write(line)

    def get(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        candidates: list[Path] = []
        if tenant_id is not None:
            candidate = self._get_run_file(tenant_id, run_id)
            if candidate.exists():
                candidates.append(candidate)
        else:
            # Search across all tenant directories for this run_id
            if self.base_dir.exists():
                candidates = list(self.base_dir.glob(f"*/{run_id}.jsonl"))

        for file_path in candidates:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("evidence_id") == evidence_id:
                        if data.get("run_id") != run_id:
                            return None
                        if tenant_id is not None and data.get("tenant_id") != tenant_id:
                            return None
                        return EvidenceRecord.model_validate(data)
        return None

    def list_for_run(self, run_id: str, tenant_id: str | None = None) -> list[EvidenceRecord]:
        results: list[EvidenceRecord] = []
        candidates: list[Path] = []
        if tenant_id is not None:
            candidate = self._get_run_file(tenant_id, run_id)
            if candidate.exists():
                candidates.append(candidate)
        else:
            if self.base_dir.exists():
                candidates = list(self.base_dir.glob(f"*/{run_id}.jsonl"))

        for file_path in candidates:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("run_id") == run_id:
                        if tenant_id is None or data.get("tenant_id") == tenant_id:
                            results.append(EvidenceRecord.model_validate(data))
        return results

    def exists(self, evidence_id: str, run_id: str, tenant_id: str | None = None) -> bool:
        return self.get(evidence_id, run_id, tenant_id) is not None


def get_default_evidence_store() -> EvidenceStore:
    """Factory creating an EvidenceStore based on EVIDENCE_STORE environment variable.

    Supported values:
    - 'postgres': PostgresEvidenceStore (default in production/evals when configured)
    - 'memory': InMemoryEvidenceStore
    - 'jsonl': JsonlEvidenceStore (uses base_dir: evals/artifacts/evidence_jsonl)
    """
    store_type = os.getenv("EVIDENCE_STORE", "memory").lower().strip()
    if store_type == "postgres":
        from tools.common.postgres_evidence_store import PostgresEvidenceStore

        return PostgresEvidenceStore()
    elif store_type == "jsonl":
        return JsonlEvidenceStore(base_dir=Path("evals/artifacts/evidence_jsonl"))
    return InMemoryEvidenceStore()
