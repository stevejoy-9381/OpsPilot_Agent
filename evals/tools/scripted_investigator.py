"""Scripted investigator evaluation harness.

Runs deterministic, non-LLM investigations for scenarios S1-S5 using the tool registry,
measures response size/tokens/latencies per tool, runs tool ablations, and validates
diagnoses with the ground truth oracle.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.common.evidence_store import InMemoryEvidenceStore
from tools.common.models import ToolContext, ToolSuccess
from tools.read.backends.snapshot import SnapshotBackend
from tools.registry import ToolRegistry, registry


@dataclass
class ToolMeasurement:
    tool: str
    chars: int
    tokens: int
    duration_ms: float


@dataclass
class InvestigationResult:
    scenario_id: str
    diagnosed: bool
    service: str
    category: str
    mechanism: str
    tool_calls_count: int
    tool_calls: list[str]
    evidence_ids: list[str]
    measurements: list[ToolMeasurement] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Investigation Policies per Scenario
# ---------------------------------------------------------------------------
async def investigate_s1(reg: ToolRegistry, ctx: ToolContext, backend: SnapshotBackend, store: InMemoryEvidenceStore, disabled_tool: str | None = None) -> tuple[dict[str, str], list[str], list[ToolMeasurement]]:
    """S1: Connection Pool Exhaustion on Orders."""
    evidence_ids: list[str] = []
    measurements: list[ToolMeasurement] = []

    # 1. get_metrics: check orders request_rate / error_rate
    if disabled_tool != "get_metrics":
        t0 = time.monotonic()
        r1 = await reg.dispatch("get_metrics", {"service": "orders", "metric": "db_pool_in_use"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r1, ToolSuccess):
            evidence_ids.append(r1.evidence_id)
            ch = len(r1.model_dump_json())
            measurements.append(ToolMeasurement("get_metrics", ch, max(1, ch // 4), dur))

    # 2. search_logs: check orders error logs
    if disabled_tool != "search_logs":
        t0 = time.monotonic()
        r2 = await reg.dispatch("search_logs", {"service": "orders", "level": "error", "limit": 5}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r2, ToolSuccess):
            evidence_ids.append(r2.evidence_id)
            ch = len(r2.model_dump_json())
            measurements.append(ToolMeasurement("search_logs", ch, max(1, ch // 4), dur))

    # 3. list_deploys: suspect recent deploy
    if disabled_tool != "list_deploys":
        t0 = time.monotonic()
        r3 = await reg.dispatch("list_deploys", {"service": "orders", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r3, ToolSuccess):
            evidence_ids.append(r3.evidence_id)
            ch = len(r3.model_dump_json())
            measurements.append(ToolMeasurement("list_deploys", ch, max(1, ch // 4), dur))

    # 4. get_deploy_diff: inspect culprit commit
    if disabled_tool != "get_deploy_diff":
        t0 = time.monotonic()
        r4 = await reg.dispatch("get_deploy_diff", {"service": "orders", "old_version": "v1.1.0", "new_version": "v1.2.0"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r4, ToolSuccess):
            evidence_ids.append(r4.evidence_id)
            ch = len(r4.model_dump_json())
            measurements.append(ToolMeasurement("get_deploy_diff", ch, max(1, ch // 4), dur))

    # If get_deploy_diff was removed or get_metrics was removed, diagnosis fails
    if disabled_tool in {"get_deploy_diff", "get_metrics"}:
        return {}, evidence_ids, measurements

    diagnosis = {
        "service": "orders",
        "category": "resource_exhaustion",
        "mechanism": "Database connection pool exhausted due to misconfigured connection pool size in v1.2.0 deploy",
    }
    return diagnosis, evidence_ids, measurements


async def investigate_s2(reg: ToolRegistry, ctx: ToolContext, backend: SnapshotBackend, store: InMemoryEvidenceStore, disabled_tool: str | None = None) -> tuple[dict[str, str], list[str], list[ToolMeasurement]]:
    """S2: Latency Regression (N+1 queries) on Inventory."""
    evidence_ids: list[str] = []
    measurements: list[ToolMeasurement] = []

    # 1. get_metrics: check inventory latency_p95
    if disabled_tool != "get_metrics":
        t0 = time.monotonic()
        r1 = await reg.dispatch("get_metrics", {"service": "inventory", "metric": "latency_p95"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r1, ToolSuccess):
            evidence_ids.append(r1.evidence_id)
            ch = len(r1.model_dump_json())
            measurements.append(ToolMeasurement("get_metrics", ch, max(1, ch // 4), dur))

    # 2. get_trace (candidates): find candidate slow traces on inventory
    if disabled_tool != "get_trace":
        t0 = time.monotonic()
        r2 = await reg.dispatch("get_trace", {"service": "inventory", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r2, ToolSuccess):
            evidence_ids.append(r2.evidence_id)
            ch = len(r2.model_dump_json())
            measurements.append(ToolMeasurement("get_trace", ch, max(1, ch // 4), dur))

        # 3. get_trace (full): inspect slow trace
        t0 = time.monotonic()
        r3 = await reg.dispatch("get_trace", {"trace_id": "tr_s2_nplus1"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r3, ToolSuccess):
            evidence_ids.append(r3.evidence_id)
            ch = len(r3.model_dump_json())
            measurements.append(ToolMeasurement("get_trace", ch, max(1, ch // 4), dur))

    # 4. list_deploys on inventory
    if disabled_tool != "list_deploys":
        t0 = time.monotonic()
        r4 = await reg.dispatch("list_deploys", {"service": "inventory", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r4, ToolSuccess):
            evidence_ids.append(r4.evidence_id)
            ch = len(r4.model_dump_json())
            measurements.append(ToolMeasurement("list_deploys", ch, max(1, ch // 4), dur))

    # 5. get_deploy_diff on inventory
    if disabled_tool != "get_deploy_diff":
        t0 = time.monotonic()
        r5 = await reg.dispatch("get_deploy_diff", {"service": "inventory", "old_version": "v2.0.0", "new_version": "v2.1.0"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r5, ToolSuccess):
            evidence_ids.append(r5.evidence_id)
            ch = len(r5.model_dump_json())
            measurements.append(ToolMeasurement("get_deploy_diff", ch, max(1, ch // 4), dur))

    if disabled_tool in {"get_trace", "get_deploy_diff"}:
        return {}, evidence_ids, measurements

    diagnosis = {
        "service": "inventory",
        "category": "latency_regression",
        "mechanism": "N+1 database query loop introduced in inventory items lookup v2.1.0",
    }
    return diagnosis, evidence_ids, measurements


async def investigate_s3(reg: ToolRegistry, ctx: ToolContext, backend: SnapshotBackend, store: InMemoryEvidenceStore, disabled_tool: str | None = None) -> tuple[dict[str, str], list[str], list[ToolMeasurement]]:
    """S3: Downstream Redis / Supplier Timeout on Inventory."""
    evidence_ids: list[str] = []
    measurements: list[ToolMeasurement] = []

    # 1. get_metrics: check inventory error_rate
    if disabled_tool != "get_metrics":
        t0 = time.monotonic()
        r1 = await reg.dispatch("get_metrics", {"service": "inventory", "metric": "error_rate"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r1, ToolSuccess):
            evidence_ids.append(r1.evidence_id)
            ch = len(r1.model_dump_json())
            measurements.append(ToolMeasurement("get_metrics", ch, max(1, ch // 4), dur))

    # 2. search_logs on inventory
    if disabled_tool != "search_logs":
        t0 = time.monotonic()
        r2 = await reg.dispatch("search_logs", {"service": "inventory", "level": "error", "limit": 5}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r2, ToolSuccess):
            evidence_ids.append(r2.evidence_id)
            ch = len(r2.model_dump_json())
            measurements.append(ToolMeasurement("search_logs", ch, max(1, ch // 4), dur))

    # 3. get_trace on S3 timeout trace
    if disabled_tool != "get_trace":
        t0 = time.monotonic()
        r3 = await reg.dispatch("get_trace", {"trace_id": "tr_s3_timeout"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r3, ToolSuccess):
            evidence_ids.append(r3.evidence_id)
            ch = len(r3.model_dump_json())
            measurements.append(ToolMeasurement("get_trace", ch, max(1, ch // 4), dur))

    # 4. list_deploys on inventory (confirms no deploy)
    if disabled_tool != "list_deploys":
        t0 = time.monotonic()
        r4 = await reg.dispatch("list_deploys", {"service": "inventory", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r4, ToolSuccess):
            evidence_ids.append(r4.evidence_id)
            ch = len(r4.model_dump_json())
            measurements.append(ToolMeasurement("list_deploys", ch, max(1, ch // 4), dur))

    if disabled_tool in {"search_logs", "get_trace"}:
        return {}, evidence_ids, measurements

    diagnosis = {
        "service": "inventory",
        "category": "dependency_timeout",
        "mechanism": "External dependency timeout contacting warehouse supplier / redis lock",
    }
    return diagnosis, evidence_ids, measurements


async def investigate_s4(reg: ToolRegistry, ctx: ToolContext, backend: SnapshotBackend, store: InMemoryEvidenceStore, disabled_tool: str | None = None) -> tuple[dict[str, str], list[str], list[ToolMeasurement]]:
    """S4: Dependency Timeout with Benign Decoy Deploy."""
    evidence_ids: list[str] = []
    measurements: list[ToolMeasurement] = []

    # 1. get_metrics
    if disabled_tool != "get_metrics":
        t0 = time.monotonic()
        r1 = await reg.dispatch("get_metrics", {"service": "inventory", "metric": "error_rate"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r1, ToolSuccess):
            evidence_ids.append(r1.evidence_id)
            ch = len(r1.model_dump_json())
            measurements.append(ToolMeasurement("get_metrics", ch, max(1, ch // 4), dur))

    # 2. search_logs on inventory
    if disabled_tool != "search_logs":
        t0 = time.monotonic()
        r2 = await reg.dispatch("search_logs", {"service": "inventory", "level": "error", "limit": 5}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r2, ToolSuccess):
            evidence_ids.append(r2.evidence_id)
            ch = len(r2.model_dump_json())
            measurements.append(ToolMeasurement("search_logs", ch, max(1, ch // 4), dur))

    # 3. list_deploys: finds decoy deploy on orders
    if disabled_tool != "list_deploys":
        t0 = time.monotonic()
        r3 = await reg.dispatch("list_deploys", {"service": "orders", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r3, ToolSuccess):
            evidence_ids.append(r3.evidence_id)
            ch = len(r3.model_dump_json())
            measurements.append(ToolMeasurement("list_deploys", ch, max(1, ch // 4), dur))

    # 4. get_deploy_diff: verifies orders deploy is benign decoy
    if disabled_tool != "get_deploy_diff":
        t0 = time.monotonic()
        r4 = await reg.dispatch("get_deploy_diff", {"service": "orders", "old_version": "v1.2.0", "new_version": "v1.2.1"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r4, ToolSuccess):
            evidence_ids.append(r4.evidence_id)
            ch = len(r4.model_dump_json())
            measurements.append(ToolMeasurement("get_deploy_diff", ch, max(1, ch // 4), dur))

    # 5. get_trace: proves true culprit is inventory upstream timeout
    if disabled_tool != "get_trace":
        t0 = time.monotonic()
        r5 = await reg.dispatch("get_trace", {"trace_id": "tr_s3_timeout"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r5, ToolSuccess):
            evidence_ids.append(r5.evidence_id)
            ch = len(r5.model_dump_json())
            measurements.append(ToolMeasurement("get_trace", ch, max(1, ch // 4), dur))

    if disabled_tool in {"get_trace", "get_deploy_diff", "search_logs"}:
        return {}, evidence_ids, measurements

    diagnosis = {
        "service": "inventory",
        "category": "dependency_timeout",
        "mechanism": "Dependency timeout on inventory; orders deploy v1.2.1 ruled out as benign decoy banner change",
    }
    return diagnosis, evidence_ids, measurements


async def investigate_s5(reg: ToolRegistry, ctx: ToolContext, backend: SnapshotBackend, store: InMemoryEvidenceStore, disabled_tool: str | None = None) -> tuple[dict[str, str], list[str], list[ToolMeasurement]]:
    """S5: False Alarm / Transient Spike Recovered."""
    evidence_ids: list[str] = []
    measurements: list[ToolMeasurement] = []

    # 1. get_metrics: check db_pool_in_use trend (flat / recovered)
    if disabled_tool != "get_metrics":
        t0 = time.monotonic()
        r1 = await reg.dispatch("get_metrics", {"service": "orders", "metric": "db_pool_in_use"}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r1, ToolSuccess):
            evidence_ids.append(r1.evidence_id)
            ch = len(r1.model_dump_json())
            measurements.append(ToolMeasurement("get_metrics", ch, max(1, ch // 4), dur))

    # 2. search_logs: check for persistent errors (none found)
    if disabled_tool != "search_logs":
        t0 = time.monotonic()
        r2 = await reg.dispatch("search_logs", {"service": "orders", "level": "error", "limit": 5}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r2, ToolSuccess):
            evidence_ids.append(r2.evidence_id)
            ch = len(r2.model_dump_json())
            measurements.append(ToolMeasurement("search_logs", ch, max(1, ch // 4), dur))

    # 3. list_deploys: verify no recent suspicious deploy
    if disabled_tool != "list_deploys":
        t0 = time.monotonic()
        r3 = await reg.dispatch("list_deploys", {"service": "orders", "limit": 3}, ctx, backend=backend, store=store)
        dur = (time.monotonic() - t0) * 1000.0
        if isinstance(r3, ToolSuccess):
            evidence_ids.append(r3.evidence_id)
            ch = len(r3.model_dump_json())
            measurements.append(ToolMeasurement("list_deploys", ch, max(1, ch // 4), dur))

    if disabled_tool == "get_metrics":
        return {}, evidence_ids, measurements

    diagnosis = {
        "service": "orders",
        "category": "false_alarm",
        "mechanism": "Transient metric spike that self-recovered to baseline; no persistent error or culprit deploy",
    }
    return diagnosis, evidence_ids, measurements


INVESTIGATORS = {
    "S1": investigate_s1,
    "S2": investigate_s2,
    "S3": investigate_s3,
    "S4": investigate_s4,
    "S5": investigate_s5,
}
