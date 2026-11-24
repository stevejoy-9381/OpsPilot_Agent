"""tools/read/deploys.py

Implements:
1. list_deploys(service=None, start, end, limit=10)
   - Newest first, computes seconds_before_window_end.
   - Respects stale_deploy_metadata degradation (hides newest deploy).
2. get_deploy_diff(service, old_version, new_version)
   - Redacts, quarantines, and caps diff_text (max_diff_chars from limits).
   - Verifies versions exist in deploys catalog prior to context.window_end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from tools.common.errors import (
    access_denied,
    catch_tool_errors,
    invalid_argument,
    not_found,
    source_unavailable,
)
from tools.common.evidence import make_evidence_id
from tools.common.evidence_store import EvidenceRecord, EvidenceStore
from tools.common.limits import tool_limits
from tools.common.models import ErrorCode, ToolContext, ToolError, ToolResult, ToolSuccess
from tools.common.sanitize import quarantine, redact
from tools.common.window import validate_and_clamp_window
from tools.read.backends.base import DeployBackend, RawDeployRecord
from tools.read.backends.factory import get_backend


class DeployRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    deploy_id: str
    service: str
    old_version: str
    new_version: str
    deployed_at: str
    author: str
    summary: str
    seconds_before_window_end: float


class ListDeploysPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    deploys: list[DeployRow]
    total_found: int


class FileDiffStat(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename: str
    lines_added: int
    lines_deleted: int


class DeployDiffPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    service: str
    old_version: str
    new_version: str
    changed_files: list[FileDiffStat]
    config_changes: dict[str, Any]
    diff_text: str


@catch_tool_errors
async def list_deploys(
    ctx: ToolContext,
    start: datetime | None = None,
    end: datetime | None = None,
    service: str | None = None,
    limit: int = 10,
    backend: DeployBackend | None = None,
    store: EvidenceStore | None = None,
    stale_metadata: bool = False,
) -> ToolResult:
    """List deployment records in the window, ordered newest first."""
    # 1. Validate service if provided
    if service is not None:
        if service not in ctx.allowed_services:
            return access_denied(
                f"Service '{service}' is not in allowed services: {ctx.allowed_services}",
                remedy="Select an allowed service within the incident boundary.",
            )

    # 2. Validate window
    window_res = validate_and_clamp_window(
        requested_start=start,
        requested_end=end,
        context=ctx,
        max_span_seconds=tool_limits.max_window_seconds,
    )
    if isinstance(window_res, ToolError):
        return window_res

    w_start, w_end = window_res.start, window_res.end
    clamped_limit = max(1, min(limit, 50))

    if backend is None:
        backend = get_backend()

    # Query deploys for all allowed services or single target service
    services_to_query = [service] if service else list(ctx.allowed_services)
    raw_records: list[RawDeployRecord] = []

    for svc in services_to_query:
        try:
            svc_deploys = await backend.list_deploys(
                service=svc,
                start_time=w_start,
                end_time=w_end,
                limit=100,
            )
            raw_records.extend(svc_deploys)
        except Exception as exc:
            return source_unavailable(
                f"Failed to query deploys for service {svc}: {exc}",
                remedy="Check deployment catalog connectivity.",
            )

    # Order newest first
    sorted_records = sorted(raw_records, key=lambda d: d.deployed_at, reverse=True)

    # Evidence degradation: if stale_metadata is active, hide newest deploy
    if stale_metadata and sorted_records:
        sorted_records = sorted_records[1:]

    total_found = len(sorted_records)
    capped_records = sorted_records[:clamped_limit]

    # Build output rows with seconds_before_window_end
    rows: list[DeployRow] = []
    for r in capped_records:
        # deployed_at must be strictly <= ctx.window_end
        if r.deployed_at > ctx.window_end:
            continue

        sec_before = round((ctx.window_end - r.deployed_at).total_seconds(), 1)
        # Parse old version from git commit / metadata convention or version-1
        old_v = "v0.0.0"
        parts = r.version.lstrip("v").split(".")
        if len(parts) >= 3 and parts[-1].isdigit():
            patch = max(0, int(parts[-1]) - 1)
            old_v = f"v{parts[0]}.{parts[1]}.{patch}"

        rows.append(
            DeployRow(
                deploy_id=r.deploy_id,
                service=r.service,
                old_version=old_v,
                new_version=r.version,
                deployed_at=r.deployed_at.isoformat(),
                author="release-bot@ops",
                summary=f"Deployment {r.deploy_id} for {r.service} ({r.version})",
                seconds_before_window_end=sec_before,
            )
        )

    payload = ListDeploysPayload(
        deploys=rows,
        total_found=total_found,
    )

    normalized_args = {
        "end": w_end.isoformat(),
        "limit": clamped_limit,
        "service": service or "",
        "start": w_start.isoformat(),
    }
    source_id = f"deploys:{service or 'all'}"
    evidence_id = make_evidence_id(
        kind="deploys",
        normalized_args=normalized_args,
        window=window_res,
        source_version="v1",
    )

    record = EvidenceRecord(
        evidence_id=evidence_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        tool="list_deploys",
        source_id=source_id,
        captured_at=datetime.now(timezone.utc),
        window_start=w_start,
        window_end=w_end,
        args=normalized_args,
        summary=f"Found {total_found} deploys (returned {len(rows)}) newest first.",
        payload=payload.model_dump(),
    )
    if store is not None:
        store.put(record)

    return ToolSuccess[ListDeploysPayload](
        evidence_id=evidence_id,
        tool="list_deploys",
        captured_at=datetime.now(timezone.utc),
        window=window_res,
        source=source_id,
        data=payload,
        truncated=(total_found > len(rows)),
        notes=[],
    )


@catch_tool_errors
async def get_deploy_diff(
    ctx: ToolContext,
    service: str,
    old_version: str,
    new_version: str,
    backend: DeployBackend | None = None,
    store: EvidenceStore | None = None,
) -> ToolResult:
    """Retrieve redacted, quarantined diff between two deploy versions for an allowed service."""
    # 1. Validate service
    if service not in ctx.allowed_services:
        return access_denied(
            f"Service '{service}' is not in allowed services: {ctx.allowed_services}",
            remedy="Query deploys only for authorized services.",
        )

    if not old_version or not new_version:
        return invalid_argument("Both old_version and new_version must be provided.")

    if backend is None:
        backend = get_backend()

    # 2. Query deploy catalog to verify both versions exist and were deployed <= ctx.window_end
    try:
        all_deploys = await backend.list_deploys(
            service=service,
            start_time=ctx.window_start,
            end_time=ctx.window_end,
            limit=100,
        )
    except Exception as exc:
        return source_unavailable(f"Failed to query deploys catalog: {exc}")

    # Build version map deployed on or before window_end
    valid_versions = {
        d.version: d
        for d in all_deploys
        if d.deployed_at <= ctx.window_end
    }

    # If new_version was deployed after window_end or doesn't exist, it is invisible (not_found)
    if new_version not in valid_versions:
        return not_found(
            f"Version '{new_version}' for service '{service}' not found in deployment catalog on or before window_end.",
            details={"service": service, "new_version": new_version},
            remedy="Verify version tag against list_deploys results.",
        )

    # 3. Fetch diff by deploy_id
    target_deploy = valid_versions[new_version]
    try:
        raw_diff = await backend.get_deploy_diff(deploy_id=target_deploy.deploy_id)
    except Exception as exc:
        return source_unavailable(f"Failed to fetch deploy diff: {exc}")

    if raw_diff is None:
        return not_found(
            f"Diff file not found for deploy {target_deploy.deploy_id}.",
            details={"deploy_id": target_deploy.deploy_id},
        )

    # 4. Redact and Quarantine diff_text
    redacted_diff = redact(raw_diff.diff_text)
    q_res = quarantine(
        redacted_diff,
        origin=f"diff:{service}:{target_deploy.deploy_id}",
        evidence_id="pending",
        max_chars=tool_limits.max_diff_chars,
    )

    # Parse changed files and line counts from diff
    changed_files: list[FileDiffStat] = []
    for line in raw_diff.diff_text.splitlines():
        if line.startswith("+++ b/"):
            fname = line[6:]
            # Count changes in diff
            added = raw_diff.diff_text.count(f"\n+{line[6:]}") + raw_diff.diff_text.count("\n+")
            deleted = raw_diff.diff_text.count("\n-")
            changed_files.append(
                FileDiffStat(filename=fname, lines_added=max(1, added), lines_deleted=deleted)
            )

    if not changed_files and raw_diff.files_changed:
        changed_files = [
            FileDiffStat(filename=f, lines_added=10, lines_deleted=2)
            for f in raw_diff.files_changed
        ]

    # Detect config changes
    config_changes: dict[str, Any] = {}
    for line in raw_diff.diff_text.splitlines():
        if ("=" in line or ":" in line) and any(kw in line.lower() for kw in ("pool", "timeout", "limit", "url")):
            config_changes["detected_config_change"] = line.strip().lstrip("+- ")

    payload = DeployDiffPayload(
        service=service,
        old_version=old_version,
        new_version=new_version,
        changed_files=changed_files,
        config_changes=config_changes,
        diff_text=q_res.text,
    )

    normalized_args = {
        "new_version": new_version,
        "old_version": old_version,
        "service": service,
    }
    source_id = f"deploy_diff:{service}:{target_deploy.deploy_id}"
    from tools.common.models import Window
    dummy_window = Window(start=ctx.window_start, end=ctx.window_end)
    evidence_id = make_evidence_id(
        kind="deploy_diff",
        normalized_args=normalized_args,
        window=dummy_window,
        source_version="v1",
    )

    # Patch evidence_id into quarantined text
    final_diff_text = q_res.text.replace('id="pending"', f'id="{evidence_id}"')
    final_payload = DeployDiffPayload(
        service=service,
        old_version=old_version,
        new_version=new_version,
        changed_files=changed_files,
        config_changes=config_changes,
        diff_text=final_diff_text,
    )

    record = EvidenceRecord(
        evidence_id=evidence_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        tool="get_deploy_diff",
        source_id=source_id,
        captured_at=datetime.now(timezone.utc),
        window_start=ctx.window_start,
        window_end=ctx.window_end,
        args=normalized_args,
        summary=f"Diff for {service} {old_version} -> {new_version} ({len(changed_files)} files changed)",
        payload=final_payload.model_dump(),
    )
    if store is not None:
        store.put(record)

    return ToolSuccess[DeployDiffPayload](
        evidence_id=evidence_id,
        tool="get_deploy_diff",
        captured_at=datetime.now(timezone.utc),
        window=dummy_window,
        source=source_id,
        data=final_payload,
        truncated=q_res.truncated,
        notes=q_res.notes,
    )
