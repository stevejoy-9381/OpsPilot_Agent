"""Tests for PostgresEvidenceStore.

Requirements tested:
1. Parity between InMemoryEvidenceStore and PostgresEvidenceStore (get, exists, list_for_run).
2. Idempotent put() via ON CONFLICT (run_id, evidence_id) DO NOTHING.
3. Conflict detection: put() with different content_hash on same (run_id, evidence_id) raises EvidenceConflict.
4. Large payload handling: payloads > 64KB offloaded to artifacts folder with payload_ref and sha256;
   payload rehydrated seamlessly on get()/list_for_run().
5. Cross-tenant and cross-run isolation: explicit predicates and PostgreSQL RLS reject cross-tenant reads
   even if application predicate were removed (tested using a raw psycopg session with different app.tenant_id).
6. verify_run_evidence: reports clean status on untampered evidence, detects modified payloads or hashes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tools.common.evidence_store import (
    EvidenceRecord,
    InMemoryEvidenceStore,
)
from tools.common.postgres_evidence_store import (
    EvidenceConflict,
    PostgresEvidenceStore,
    verify_run_evidence,
)

DB_URL = "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"
RAW_PG_URL = "postgresql://opspilot@127.0.0.1:54333/opspilot"


@pytest.fixture(scope="module")
def session_factory():
    engine = create_engine(DB_URL, pool_size=5)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def tenant_and_run(session_factory):
    tenant_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    inc_id = str(uuid.uuid4())

    with psycopg.connect(RAW_PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
                (tenant_id, "Test Tenant", f"t-{tenant_id[:8]}"),
            )
            cur.execute(
                "INSERT INTO app.incidents (id, tenant_id, alert_id, source, service, severity, alert_payload, opened_at, status_summary) "
                "VALUES (%s, %s, 'a1', 'alert', 'orders', 'HIGH', '{}'::jsonb, now(), 'open')",
                (inc_id, tenant_id),
            )
            cur.execute(
                "INSERT INTO app.runs (id, tenant_id, incident_id, status, version, remaining_calls, max_calls) "
                "VALUES (%s, %s, %s, 'investigating', 1, 10, 10)",
                (run_id, tenant_id, inc_id),
            )
            cur.execute(
                "INSERT INTO app.run_events (run_id, tenant_id, seq, type, actor, payload) "
                "VALUES (%s, %s, 1, 'run_created', 'system', '{}'::jsonb)",
                (run_id, tenant_id),
            )
    return tenant_id, run_id


def _make_record(
    evidence_id: str,
    run_id: str,
    tenant_id: str,
    summary: str = "Latency spike p95=350ms",
    payload: Any = {"metrics": [10, 20, 30]},
    tool: str = "get_metrics",
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        tenant_id=tenant_id,
        tool=tool,
        source_id="prom:orders:latency",
        captured_at=datetime.now(timezone.utc),
        window_start=datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 11, 24, 10, 15, 0, tzinfo=timezone.utc),
        args={"service": "orders", "metric": "latency_p95"},
        summary=summary,
        payload=payload,
    )


def test_parity_between_memory_and_postgres_stores(session_factory, tenant_and_run, tmp_path):
    """Verifies behavioral parity between InMemoryEvidenceStore and PostgresEvidenceStore."""
    tenant_id, run_id = tenant_and_run

    mem_store = InMemoryEvidenceStore()
    pg_store = PostgresEvidenceStore(session_factory=session_factory, artifacts_dir=tmp_path)

    rec1 = _make_record("ev_001", run_id, tenant_id, summary="rec 1", payload={"v": 1})
    rec2 = _make_record("ev_002", run_id, tenant_id, summary="rec 2", payload={"v": 2})

    for store in (mem_store, pg_store):
        store.put(rec1)
        store.put(rec2)

        # get
        g1 = store.get("ev_001", run_id, tenant_id)
        assert g1 is not None
        assert g1.evidence_id == "ev_001"
        assert g1.payload == {"v": 1}

        # exists
        assert store.exists("ev_001", run_id, tenant_id) is True
        assert store.exists("ev_999", run_id, tenant_id) is False

        # list_for_run
        items = store.list_for_run(run_id, tenant_id)
        assert len(items) == 2
        ids = {it.evidence_id for it in items}
        assert ids == {"ev_001", "ev_002"}

        # Cross-run isolation
        assert store.get("ev_001", run_id="other_run", tenant_id=tenant_id) is None


def test_idempotent_put_and_conflict_detection(session_factory, tenant_and_run, tmp_path):
    """Identical put() is a no-op; put() with different content_hash raises EvidenceConflict."""
    tenant_id, run_id = tenant_and_run
    pg_store = PostgresEvidenceStore(session_factory=session_factory, artifacts_dir=tmp_path)

    rec = _make_record("ev_idempotent", run_id, tenant_id, payload={"count": 42})
    pg_store.put(rec)

    # 1. Identical re-insert succeeds idempotently without error
    pg_store.put(rec)
    fetched = pg_store.get("ev_idempotent", run_id, tenant_id)
    assert fetched is not None
    assert fetched.payload == {"count": 42}

    # 2. Conflicting re-insert with different payload on same (run_id, evidence_id) raises EvidenceConflict
    conflicting_rec = _make_record("ev_idempotent", run_id, tenant_id, payload={"count": 999})
    assert conflicting_rec.content_hash != rec.content_hash

    with pytest.raises(EvidenceConflict) as exc:
        pg_store.put(conflicting_rec)
    assert "Evidence conflict" in str(exc.value)
    assert "non-deterministic tool output detected" in str(exc.value)


def test_large_payload_artifact_offloading(session_factory, tenant_and_run, tmp_path):
    """Payloads exceeding size cap are written to artifact files and rehydrated transparently."""
    tenant_id, run_id = tenant_and_run
    # Set small size cap of 500 bytes to test offloading deterministically
    pg_store = PostgresEvidenceStore(
        session_factory=session_factory,
        payload_size_cap_bytes=500,
        artifacts_dir=tmp_path,
    )

    large_data = {"log_lines": [f"Log event number {i} with metadata payload padding" for i in range(100)]}
    rec = _make_record("ev_large", run_id, tenant_id, summary="100 log lines", payload=large_data)

    pg_store.put(rec)

    # Verify that an artifact file was created in tmp_path
    artifacts = list(tmp_path.glob("*.json"))
    assert len(artifacts) == 1
    sha = artifacts[0].stem
    assert len(sha) == 64

    # Fetch through store: payload should be completely rehydrated
    fetched = pg_store.get("ev_large", run_id, tenant_id)
    assert fetched is not None
    assert fetched.payload == large_data
    assert fetched.payload_ref is not None
    assert sha in fetched.payload_ref

    # Raw database check: db row has payload_ref and offloaded metadata
    with psycopg.connect(RAW_PG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload, payload_ref FROM app.observations WHERE evidence_id = 'ev_large'"
            )
            row = cur.fetchone()
            assert row[1] is not None
            assert "offloaded_sha256" in row[0]


def test_cross_tenant_isolation_via_rls(session_factory, tenant_and_run, tmp_path):
    """Verifies cross-tenant read is blocked by PostgreSQL RLS even without app predicate."""
    tenant_id, run_id = tenant_and_run
    other_tenant_id = str(uuid.uuid4())

    with psycopg.connect(RAW_PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.tenants (id, name, slug) VALUES (%s, %s, %s)",
                (other_tenant_id, "Tenant Other", f"t-{other_tenant_id[:8]}"),
            )

    pg_store = PostgresEvidenceStore(session_factory=session_factory, artifacts_dir=tmp_path)
    rec = _make_record("ev_tenant_test", run_id, tenant_id, payload={"secret": "alpha"})
    pg_store.put(rec)

    # 1. Store API get with wrong tenant returns None
    assert pg_store.get("ev_tenant_test", run_id, tenant_id=other_tenant_id) is None

    # 2. Raw SQL query as opspilot_api role with app.tenant_id set to other_tenant_id
    with psycopg.connect(RAW_PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE opspilot_api")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (other_tenant_id,))

            # Query without any WHERE tenant_id predicate
            cur.execute("SELECT evidence_id FROM app.observations WHERE evidence_id = 'ev_tenant_test'")
            assert cur.fetchone() is None  # RLS filters it out completely!


def test_verify_run_evidence_clean_and_tampered(session_factory, tenant_and_run, tmp_path):
    """verify_run_evidence verifies intact hashes and flags tampered payloads."""
    tenant_id, run_id = tenant_and_run
    pg_store = PostgresEvidenceStore(session_factory=session_factory, artifacts_dir=tmp_path)

    rec1 = _make_record("ev_v1", run_id, tenant_id, payload={"status": "green"})
    rec2 = _make_record("ev_v2", run_id, tenant_id, payload={"status": "yellow"})
    pg_store.put(rec1)
    pg_store.put(rec2)

    # 1. Clean verification report
    report = verify_run_evidence(run_id=run_id, tenant_id=tenant_id)
    assert report["clean"] is True
    assert report["total_records"] == 2
    assert len(report["mismatches"]) == 0

    # 2. Tampering test: simulate an out-of-band row update (bypassing append-only via migrator)
    with psycopg.connect(RAW_PG_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Temporarily disable append-only trigger for this tampering test
            cur.execute("ALTER TABLE app.observations DISABLE TRIGGER trg_observations_append_only")
            cur.execute(
                "UPDATE app.observations SET payload = '{\"status\": \"red_tampered\"}'::jsonb WHERE evidence_id = 'ev_v1'"
            )
            cur.execute("ALTER TABLE app.observations ENABLE TRIGGER trg_observations_append_only")

    # 3. Report flags tampered evidence
    tampered_report = verify_run_evidence(run_id=run_id, tenant_id=tenant_id)
    assert tampered_report["clean"] is False
    assert len(tampered_report["mismatches"]) == 1
    mismatch = tampered_report["mismatches"][0]
    assert mismatch["evidence_id"] == "ev_v1"
    assert mismatch["stored_hash"] != mismatch["expected_hash"]
