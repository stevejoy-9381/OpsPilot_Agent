"""PostgreSQL implementation of EvidenceStore protocol with RLS and artifact offloading.

Requirements:
1. put() inserts into app.observations, idempotent via ON CONFLICT (run_id, evidence_id) DO NOTHING;
   if existing row's content_hash differs, raises EvidenceConflict (non-deterministic tool detection).
2. get(), exists(), list_for_run() are tenant- and run-scoped through RLS plus explicit predicates.
   Cross-tenant and cross-run reads return None.
3. Large payloads: jsonb payload stored directly up to PAYLOAD_SIZE_CAP (default: 64KB).
   Above it, summary + payload_ref are stored, with raw payload persisted to evidence artifacts folder
   named by sha256 digest.
4. verify_run_evidence(run_id, tenant_id=None, session=None):
   Recomputes content_hash for every row in the run and verifies equality.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tools.common.evidence_store import EvidenceRecord, compute_content_hash

logger = logging.getLogger(__name__)

# Size cap: 64KB (65,536 bytes) of serialized UTF-8 JSON
DEFAULT_PAYLOAD_SIZE_CAP_BYTES = 64 * 1024
DEFAULT_ARTIFACTS_DIR = Path("evals/artifacts/evidence")


class EvidenceConflict(Exception):
    """Raised when put() is called with an identical (run_id, evidence_id) but different content_hash."""

    def __init__(
        self,
        run_id: str,
        evidence_id: str,
        existing_hash: str,
        new_hash: str,
    ) -> None:
        super().__init__(
            f"Evidence conflict for run '{run_id}', evidence_id '{evidence_id}': "
            f"existing content_hash '{existing_hash}' != new content_hash '{new_hash}' "
            "(non-deterministic tool output detected)"
        )
        self.run_id = run_id
        self.evidence_id = evidence_id
        self.existing_hash = existing_hash
        self.new_hash = new_hash


def _coerce_uuid(val: Any) -> uuid.UUID:
    """Safely converts string or UUID to uuid.UUID."""
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except Exception:
        # Generate deterministic UUID5 namespace if not a standard UUID string (e.g. 'run_001')
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(val))


def _normalize_sync_url(url: str) -> str:
    """Ensures database URL uses synchronous psycopg driver."""
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class PostgresEvidenceStore:
    """PostgreSQL-backed EvidenceStore implementing the Feature 2.4 protocol."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        db_url: str | None = None,
        payload_size_cap_bytes: int = DEFAULT_PAYLOAD_SIZE_CAP_BYTES,
        artifacts_dir: Path | str = DEFAULT_ARTIFACTS_DIR,
    ) -> None:
        self.payload_size_cap_bytes = payload_size_cap_bytes
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if session_factory is not None:
            self._session_factory = session_factory
        else:
            raw_url = db_url or os.getenv("DATABASE_URL") or "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"
            url = _normalize_sync_url(raw_url)
            engine = create_engine(url, pool_size=10, max_overflow=5)
            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_session(self) -> Session:
        session: Session = self._session_factory()
        return session

    def _set_tenant_context(self, session: Session, tenant_id: uuid.UUID) -> None:
        """Sets transaction-local app.tenant_id for PostgreSQL RLS."""
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    def put(self, record: EvidenceRecord) -> None:
        """Stores an evidence record into app.observations.

        Idempotent via ON CONFLICT (run_id, evidence_id) DO NOTHING.
        If existing content_hash differs from new content_hash, raises EvidenceConflict.
        Large payloads exceeding payload_size_cap_bytes are written to artifacts folder.
        """
        run_uuid = _coerce_uuid(record.run_id)
        tenant_uuid = _coerce_uuid(record.tenant_id)

        # Offload payload if larger than cap
        payload_to_store = record.payload
        payload_ref_to_store = record.payload_ref

        if payload_to_store is not None:
            serialized_bytes = json.dumps(payload_to_store, sort_keys=True, default=str).encode("utf-8")
            if len(serialized_bytes) > self.payload_size_cap_bytes:
                # Store artifact file named by sha256
                sha = hashlib.sha256(serialized_bytes).hexdigest()
                artifact_path = self.artifacts_dir / f"{sha}.json"
                if not artifact_path.exists():
                    artifact_path.write_bytes(serialized_bytes)
                payload_ref_to_store = str(artifact_path.as_posix())
                payload_to_store = {"summary": record.summary, "offloaded_sha256": sha}

        with self._get_session() as session:
            self._set_tenant_context(session, tenant_uuid)

            # Check for existing record to detect content_hash divergence
            check_stmt = text(
                """
                SELECT content_hash FROM app.observations
                WHERE run_id = :run_id AND evidence_id = :evidence_id
                """
            )
            existing = session.execute(
                check_stmt,
                {"run_id": run_uuid, "evidence_id": record.evidence_id},
            ).scalar()

            if existing is not None:
                if existing.strip() != record.content_hash.strip():
                    raise EvidenceConflict(
                        run_id=str(record.run_id),
                        evidence_id=record.evidence_id,
                        existing_hash=existing.strip(),
                        new_hash=record.content_hash.strip(),
                    )
                # Idempotent match: already saved
                return

            # Insert into app.observations
            insert_stmt = text(
                """
                INSERT INTO app.observations (
                    evidence_id, run_id, tenant_id, tool, source_id,
                    captured_at, window_start, window_end, args,
                    summary, payload, payload_ref, source_version, content_hash
                ) VALUES (
                    :evidence_id, :run_id, :tenant_id, :tool, :source_id,
                    :captured_at, :window_start, :window_end, :args,
                    :summary, :payload, :payload_ref, :source_version, :content_hash
                )
                ON CONFLICT (run_id, evidence_id) DO NOTHING
                """
            )
            session.execute(
                insert_stmt,
                {
                    "evidence_id": record.evidence_id,
                    "run_id": run_uuid,
                    "tenant_id": tenant_uuid,
                    "tool": record.tool,
                    "source_id": record.source_id,
                    "captured_at": record.captured_at,
                    "window_start": record.window_start,
                    "window_end": record.window_end,
                    "args": json.dumps(record.args),
                    "summary": record.summary,
                    "payload": json.dumps(payload_to_store),
                    "payload_ref": payload_ref_to_store,
                    "source_version": record.source_version,
                    "content_hash": record.content_hash,
                },
            )
            session.commit()

    def get(
        self,
        evidence_id: str,
        run_id: str,
        tenant_id: str | None = None,
    ) -> EvidenceRecord | None:
        """Retrieves an evidence record. Scoped by run_id and tenant_id."""
        run_uuid = _coerce_uuid(run_id)

        with self._get_session() as session:
            if tenant_id is not None:
                tenant_uuid = _coerce_uuid(tenant_id)
                self._set_tenant_context(session, tenant_uuid)

            query = text(
                """
                SELECT evidence_id, run_id, tenant_id, tool, source_id,
                       captured_at, window_start, window_end, args,
                       summary, payload, payload_ref, source_version, content_hash
                FROM app.observations
                WHERE run_id = :run_id AND evidence_id = :evidence_id
                """
                + (" AND tenant_id = :tenant_id" if tenant_id is not None else "")
            )
            params = {"run_id": run_uuid, "evidence_id": evidence_id}
            if tenant_id is not None:
                params["tenant_id"] = _coerce_uuid(tenant_id)

            row = session.execute(query, params).first()
            if row is None:
                return None

            return self._row_to_record(row, expected_run_id=run_id, expected_tenant_id=tenant_id)

    def list_for_run(
        self,
        run_id: str,
        tenant_id: str | None = None,
    ) -> list[EvidenceRecord]:
        """Lists all evidence records for a given run, scoped to tenant if provided."""
        run_uuid = _coerce_uuid(run_id)

        with self._get_session() as session:
            if tenant_id is not None:
                tenant_uuid = _coerce_uuid(tenant_id)
                self._set_tenant_context(session, tenant_uuid)

            query = text(
                """
                SELECT evidence_id, run_id, tenant_id, tool, source_id,
                       captured_at, window_start, window_end, args,
                       summary, payload, payload_ref, source_version, content_hash
                FROM app.observations
                WHERE run_id = :run_id
                """
                + (" AND tenant_id = :tenant_id" if tenant_id is not None else "")
                + " ORDER BY captured_at ASC"
            )
            params = {"run_id": run_uuid}
            if tenant_id is not None:
                params["tenant_id"] = _coerce_uuid(tenant_id)

            rows = session.execute(query, params).fetchall()
            return [
                self._row_to_record(r, expected_run_id=run_id, expected_tenant_id=tenant_id)
                for r in rows
            ]

    def exists(
        self,
        evidence_id: str,
        run_id: str,
        tenant_id: str | None = None,
    ) -> bool:
        """Returns True if record exists for run and tenant, else False."""
        return self.get(evidence_id, run_id, tenant_id) is not None

    def _row_to_record(
        self,
        row: Any,
        expected_run_id: str | None = None,
        expected_tenant_id: str | None = None,
    ) -> EvidenceRecord:
        """Converts database row tuple to EvidenceRecord, resolving offloaded artifacts if present."""
        payload = row.payload
        payload_ref = row.payload_ref

        # If payload was offloaded to an artifact file and payload_ref exists, rehydrate payload
        if payload_ref and Path(payload_ref).exists():
            try:
                payload = json.loads(Path(payload_ref).read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not rehydrate offloaded artifact %s: %s", payload_ref, e)

        # Preserve caller's run_id and tenant_id string format if valid UUID or original string
        out_run_id = expected_run_id if expected_run_id is not None else str(row.run_id)
        out_tenant_id = expected_tenant_id if expected_tenant_id is not None else str(row.tenant_id)

        return EvidenceRecord(
            evidence_id=str(row.evidence_id),
            run_id=out_run_id,
            tenant_id=out_tenant_id,
            tool=str(row.tool),
            source_id=str(row.source_id),
            captured_at=row.captured_at,
            window_start=row.window_start,
            window_end=row.window_end,
            args=row.args if isinstance(row.args, dict) else json.loads(row.args),
            summary=str(row.summary),
            payload=payload,
            payload_ref=payload_ref,
            source_version=str(row.source_version),
            content_hash=str(row.content_hash).strip(),
        )


def verify_run_evidence(
    run_id: str,
    tenant_id: str | None = None,
    session: Session | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Recomputes content_hash for every observation in run and verifies tampering.

    Returns:
    {
        "run_id": run_id,
        "total_records": int,
        "clean": bool,
        "mismatches": list[dict],
    }
    """
    run_uuid = _coerce_uuid(run_id)
    own_session = False
    if session is None:
        raw_url = db_url or os.getenv("DATABASE_URL") or "postgresql+psycopg://opspilot@127.0.0.1:54333/opspilot"
        url = _normalize_sync_url(raw_url)
        engine = create_engine(url)
        session = sessionmaker(bind=engine)()
        own_session = True

    try:
        if tenant_id is not None:
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(_coerce_uuid(tenant_id))},
            )

        query = text(
            """
            SELECT evidence_id, payload, payload_ref, content_hash
            FROM app.observations
            WHERE run_id = :run_id
            """
            + (" AND tenant_id = :tenant_id" if tenant_id is not None else "")
            + " ORDER BY evidence_id ASC"
        )
        params = {"run_id": run_uuid}
        if tenant_id is not None:
            params["tenant_id"] = _coerce_uuid(tenant_id)

        rows = session.execute(query, params).fetchall()

        mismatches: list[dict[str, Any]] = []
        for row in rows:
            evidence_id = str(row.evidence_id)
            stored_hash = str(row.content_hash).strip()
            payload = row.payload
            payload_ref = row.payload_ref

            if payload_ref and Path(payload_ref).exists():
                try:
                    payload = json.loads(Path(payload_ref).read_text(encoding="utf-8"))
                except Exception:
                    pass

            expected_hash = compute_content_hash(payload if payload is not None else payload_ref)
            if stored_hash != expected_hash:
                mismatches.append(
                    {
                        "evidence_id": evidence_id,
                        "stored_hash": stored_hash,
                        "expected_hash": expected_hash,
                    }
                )

        return {
            "run_id": run_id,
            "total_records": len(rows),
            "clean": len(mismatches) == 0,
            "mismatches": mismatches,
        }
    finally:
        if own_session:
            session.close()
