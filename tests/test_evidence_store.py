"""Tests for tools/common/evidence_store.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tools.common.evidence_store import (
    EvidenceRecord,
    InMemoryEvidenceStore,
    JsonlEvidenceStore,
    compute_content_hash,
)


def _make_record(
    evidence_id: str = "ev_test_1234",
    run_id: str = "run_001",
    tenant_id: str = "tenant_alpha",
    summary: str = "Error rate peak 0.42",
    payload: Any = {"metrics": [1.0, 2.0, 3.0]},
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        tenant_id=tenant_id,
        tool="get_metrics",
        source_id="prometheus:orders:error_rate",
        captured_at=datetime.now(timezone.utc),
        window_start=datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 3, 30, 10, 15, 0, tzinfo=timezone.utc),
        args={"service": "orders", "metric": "error_rate"},
        summary=summary,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# InMemoryEvidenceStore Tests
# ---------------------------------------------------------------------------
def test_in_memory_store_put_and_get() -> None:
    store = InMemoryEvidenceStore()
    record = _make_record()

    store.put(record)
    retrieved = store.get("ev_test_1234", run_id="run_001")
    assert retrieved is not None
    assert retrieved.evidence_id == "ev_test_1234"
    assert retrieved.summary == "Error rate peak 0.42"
    assert store.exists("ev_test_1234", run_id="run_001")


def test_in_memory_store_idempotent_put() -> None:
    store = InMemoryEvidenceStore()
    record1 = _make_record(summary="First summary")
    record2 = _make_record(summary="Different summary with same ID")

    store.put(record1)
    store.put(record2)  # Should not overwrite

    retrieved = store.get("ev_test_1234", run_id="run_001")
    assert retrieved is not None
    assert retrieved.summary == "First summary"


def test_in_memory_store_cross_run_and_tenant_isolation() -> None:
    store = InMemoryEvidenceStore()
    record = _make_record(run_id="run_001", tenant_id="tenant_alpha")
    store.put(record)

    # Cross-run: querying with different run_id returns None
    assert store.get("ev_test_1234", run_id="run_002") is None
    assert not store.exists("ev_test_1234", run_id="run_002")

    # Cross-tenant: querying with mismatched tenant returns None
    assert store.get("ev_test_1234", run_id="run_001", tenant_id="tenant_beta") is None
    assert not store.exists("ev_test_1234", run_id="run_001", tenant_id="tenant_beta")

    # Matching run and tenant succeeds
    assert store.get("ev_test_1234", run_id="run_001", tenant_id="tenant_alpha") is not None


# ---------------------------------------------------------------------------
# JsonlEvidenceStore Tests
# ---------------------------------------------------------------------------
def test_jsonl_store_put_and_get(tmp_path: Path) -> None:
    store = JsonlEvidenceStore(tmp_path)
    record = _make_record()

    store.put(record)
    retrieved = store.get("ev_test_1234", run_id="run_001", tenant_id="tenant_alpha")
    assert retrieved is not None
    assert retrieved.evidence_id == "ev_test_1234"
    assert retrieved.content_hash == record.content_hash

    # Idempotent put: file line count remains 1
    store.put(record)
    run_file = tmp_path / "tenant_alpha" / "run_001.jsonl"
    assert run_file.exists()
    lines = run_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


def test_jsonl_store_cross_tenant_and_run_isolation(tmp_path: Path) -> None:
    store = JsonlEvidenceStore(tmp_path)
    record = _make_record(run_id="run_001", tenant_id="tenant_alpha")
    store.put(record)

    # Mismatched tenant
    assert store.get("ev_test_1234", run_id="run_001", tenant_id="tenant_beta") is None
    # Mismatched run
    assert store.get("ev_test_1234", run_id="run_999", tenant_id="tenant_alpha") is None


def test_jsonl_store_list_for_run(tmp_path: Path) -> None:
    store = JsonlEvidenceStore(tmp_path)
    rec1 = _make_record(evidence_id="ev_001", run_id="run_abc", tenant_id="t1")
    rec2 = _make_record(evidence_id="ev_002", run_id="run_abc", tenant_id="t1")
    rec3 = _make_record(evidence_id="ev_003", run_id="run_xyz", tenant_id="t1")

    store.put(rec1)
    store.put(rec2)
    store.put(rec3)

    run_abc_records = store.list_for_run("run_abc", tenant_id="t1")
    assert len(run_abc_records) == 2
    ids = {r.evidence_id for r in run_abc_records}
    assert ids == {"ev_001", "ev_002"}


# ---------------------------------------------------------------------------
# Content Hash Alteration Detection
# ---------------------------------------------------------------------------
def test_content_hash_changes_when_payload_changes() -> None:
    payload1 = {"error_rate": 0.05, "status": "ok"}
    payload2 = {"error_rate": 0.50, "status": "critical"}

    hash1 = compute_content_hash(payload1)
    hash2 = compute_content_hash(payload2)

    assert hash1 != hash2

    record1 = _make_record(payload=payload1)
    record2 = _make_record(payload=payload2)
    assert record1.content_hash == hash1
    assert record2.content_hash == hash2
    assert record1.content_hash != record2.content_hash


# ---------------------------------------------------------------------------
# Write Failure Path
# ---------------------------------------------------------------------------
def test_write_failure_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonlEvidenceStore(tmp_path)
    record = _make_record()

    # Simulate filesystem I/O write failure
    def mock_write_error(*args: Any, **kwargs: Any) -> None:
        raise OSError("Disk full or permission denied")

    monkeypatch.setattr("builtins.open", mock_write_error)

    with pytest.raises(OSError, match="Disk full"):
        store.put(record)
