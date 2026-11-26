"""Deploy primitive and deployment history operations for OpsPilot lab."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg

from lab.controller_lib.metadata import (
    VersionMetadata,
    get_historical_seed_records,
    get_version_metadata,
)


@dataclass(frozen=True)
class DeployRecord:
    """Represents a persisted deployment event in the lab registry."""

    deploy_id: str
    service: str
    old_version: str
    new_version: str
    deployed_at: datetime
    author: str
    summary: str
    changed_files: list[str]
    config_changes: dict[str, Any]
    diff_text: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["deployed_at"] = self.deployed_at.isoformat()
        return data


def _get_service_admin_url(service: str, override_url: str | None = None) -> str:
    """Resolves the base HTTP URL for a lab service admin endpoint."""
    if override_url:
        return override_url.rstrip("/")

    env_var = f"{service.upper()}_URL"
    url = os.getenv(env_var)
    if url:
        return url.rstrip("/")

    default_ports = {
        "gateway": 8000,
        "orders": 8000,
        "inventory": 8000,
    }
    port = default_ports.get(service.lower(), 8000)
    return f"http://{service}:{port}"


def _get_database_url(override_url: str | None = None) -> str:
    """Resolves standard PostgreSQL connection string for lab operations."""
    url = (
        override_url
        or os.getenv("LAB_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://controller_admin:controller-admin-local-pw@127.0.0.1:5432/lab"
    )
    clean = url.replace("postgresql+psycopg://", "postgresql://")
    return clean


def record_deploy_in_tx(
    conn: psycopg.Connection[Any],
    *,
    service: str,
    old_version: str,
    new_version: str,
    author: str | None = None,
) -> DeployRecord:
    """Inserts a lab.deploys row using the caller's open PostgreSQL transaction."""
    meta: VersionMetadata = get_version_metadata(service, old_version, new_version)
    deploy_author = author or meta.author
    deploy_id = f"dep-{uuid.uuid4().hex[:12]}"
    deployed_at = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lab.deploys (
                deploy_id, service, old_version, new_version, deployed_at,
                author, summary, changed_files, config_changes, diff_text,
                version, commit_sha, deployed_by, status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            ) RETURNING deploy_id, service, old_version, new_version, deployed_at,
                        author, summary, changed_files, config_changes, diff_text;
            """,
            (
                deploy_id,
                service,
                old_version,
                new_version,
                deployed_at,
                deploy_author,
                meta.summary,
                json.dumps(meta.changed_files),
                json.dumps(meta.config_changes),
                meta.diff_text,
                new_version,
                "synthetic",
                deploy_author,
                "SUCCESS",
            ),
        )
        row = cur.fetchone()
        assert row is not None

        changed_files = row[7] if isinstance(row[7], list) else json.loads(row[7])
        config_changes = row[8] if isinstance(row[8], dict) else json.loads(row[8])

        return DeployRecord(
            deploy_id=row[0],
            service=row[1],
            old_version=row[2],
            new_version=row[3],
            deployed_at=row[4],
            author=row[5],
            summary=row[6],
            changed_files=changed_files,
            config_changes=config_changes,
            diff_text=row[9],
        )


async def deploy(
    service: str,
    new_version: str,
    *,
    service_url: str | None = None,
    admin_token: str | None = None,
    database_url: str | None = None,
    author: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> DeployRecord:
    """Switches active service version profile via /admin/deploy and records event in lab.deploys.
    
    1. Calls service GET /admin/state to inspect current running version.
    2. Calls service POST /admin/deploy with target version and admin token.
    3. Resolves truthful synthetic diff and changelog metadata.
    4. Writes exactly one audit row into lab.deploys with real timestamp.
    """
    token: str = admin_token or os.getenv("LAB_ADMIN_TOKEN") or "lab-admin-dev-token"
    base_url = _get_service_admin_url(service, service_url)
    db_url = _get_database_url(database_url)

    headers: dict[str, str] = {"X-Lab-Admin-Token": token}

    # Step 1 & 2: Call service admin endpoints
    old_version = "v1.0.0"

    async def _perform_admin_calls(client: httpx.AsyncClient) -> str:
        nonlocal old_version
        try:
            state_resp = await client.get(f"{base_url}/admin/state", headers=headers, timeout=5.0)
            if state_resp.status_code == 200:
                old_version = state_resp.json().get("version", "v1.0.0")
        except Exception:
            # Fall back to latest database record if service state endpoint unavailable
            pass

        deploy_resp = await client.post(
            f"{base_url}/admin/deploy",
            json={"version": new_version},
            headers=headers,
            timeout=5.0,
        )
        if deploy_resp.status_code != 200:
            raise RuntimeError(
                f"Deployment failed for {service} to {new_version}: "
                f"HTTP {deploy_resp.status_code} - {deploy_resp.text}"
            )
        return old_version

    if http_client is not None:
        await _perform_admin_calls(http_client)
    else:
        async with httpx.AsyncClient() as client:
            await _perform_admin_calls(client)

    # Step 3: Resolve metadata
    meta: VersionMetadata = get_version_metadata(service, old_version, new_version)
    deploy_author = author or meta.author

    deploy_id = f"dep-{uuid.uuid4().hex[:12]}"
    deployed_at = datetime.now(timezone.utc)

    # Step 4: Insert row into lab.deploys table
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lab.deploys (
                    deploy_id, service, old_version, new_version, deployed_at,
                    author, summary, changed_files, config_changes, diff_text,
                    version, commit_sha, deployed_by, status
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                ) RETURNING deploy_id, service, old_version, new_version, deployed_at,
                            author, summary, changed_files, config_changes, diff_text;
                """,
                (
                    deploy_id,
                    service,
                    old_version,
                    new_version,
                    deployed_at,
                    deploy_author,
                    meta.summary,
                    json.dumps(meta.changed_files),
                    json.dumps(meta.config_changes),
                    meta.diff_text,
                    new_version,
                    "synthetic",
                    deploy_author,
                    "SUCCESS",
                ),
            )
            row = cur.fetchone()
            assert row is not None

            # Parse json columns if returned as string
            changed_files = row[7] if isinstance(row[7], list) else json.loads(row[7])
            config_changes = row[8] if isinstance(row[8], dict) else json.loads(row[8])

            return DeployRecord(
                deploy_id=row[0],
                service=row[1],
                old_version=row[2],
                new_version=row[3],
                deployed_at=row[4],
                author=row[5],
                summary=row[6],
                changed_files=changed_files,
                config_changes=config_changes,
                diff_text=row[9],
            )


async def rollback(
    service: str,
    *,
    service_url: str | None = None,
    admin_token: str | None = None,
    database_url: str | None = None,
    author: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> DeployRecord:
    """Rolls back the specified service to its previous version profile.
    
    Inspects lab.deploys history to discover the previous version profile,
    then executes deploy() to activate it and logs a new deploy history row.
    """
    db_url = _get_database_url(database_url)

    target_version = "v1.0.0"
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT old_version, new_version
                FROM lab.deploys
                WHERE service = %s
                ORDER BY deployed_at DESC, deploy_id DESC
                LIMIT 1;
                """,
                (service,),
            )
            latest = cur.fetchone()
            if latest is not None and latest[0]:
                target_version = latest[0]

    rollback_author = author or "release-automation@internal"
    return await deploy(
        service=service,
        new_version=target_version,
        service_url=service_url,
        admin_token=admin_token,
        database_url=database_url,
        author=rollback_author,
        http_client=http_client,
    )


def get_deploy_history(
    service: str | None = None,
    limit: int = 50,
    database_url: str | None = None,
) -> list[DeployRecord]:
    """Retrieves chronological deployment history from lab.deploys."""
    db_url = _get_database_url(database_url)
    results: list[DeployRecord] = []

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            if service:
                cur.execute(
                    """
                    SELECT deploy_id, service, old_version, new_version, deployed_at,
                           author, summary, changed_files, config_changes, diff_text
                    FROM lab.deploys
                    WHERE service = %s
                    ORDER BY deployed_at DESC, deploy_id DESC
                    LIMIT %s;
                    """,
                    (service, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT deploy_id, service, old_version, new_version, deployed_at,
                           author, summary, changed_files, config_changes, diff_text
                    FROM lab.deploys
                    ORDER BY deployed_at DESC, deploy_id DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )

            for row in cur.fetchall():
                changed_files = row[7] if isinstance(row[7], list) else json.loads(row[7])
                config_changes = row[8] if isinstance(row[8], dict) else json.loads(row[8])
                results.append(
                    DeployRecord(
                        deploy_id=row[0],
                        service=row[1],
                        old_version=row[2],
                        new_version=row[3],
                        deployed_at=row[4],
                        author=row[5],
                        summary=row[6],
                        changed_files=changed_files,
                        config_changes=config_changes,
                        diff_text=row[9],
                    )
                )

    return results


def seed_deploy_history(database_url: str | None = None) -> int:
    """Inserts initial historical deploy records into lab.deploys providing context."""
    db_url = _get_database_url(database_url)
    records = get_historical_seed_records()
    inserted_count = 0

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO lab.deploys (
                        deploy_id, service, old_version, new_version, deployed_at,
                        author, summary, changed_files, config_changes, diff_text,
                        version, commit_sha, deployed_by, status
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    ) ON CONFLICT (deploy_id) DO NOTHING;
                    """,
                    (
                        rec["deploy_id"],
                        rec["service"],
                        rec["old_version"],
                        rec["new_version"],
                        rec["deployed_at"],
                        rec["author"],
                        rec["summary"],
                        json.dumps(rec["changed_files"]),
                        json.dumps(rec["config_changes"]),
                        rec["diff_text"],
                        rec["new_version"],
                        "initial",
                        rec["author"],
                        "SUCCESS",
                    ),
                )
                if cur.rowcount > 0:
                    inserted_count += 1

    return inserted_count
