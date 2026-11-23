from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path

from lab.loadgen.generator import generate_schedule, hash_request_sequence
from lab.loadgen.models import LoadProfile, RunSummary
from lab.loadgen.profile_loader import get_profile
from lab.loadgen.scheduler import OpenLoopScheduler


def resolve_output_path(output_path: str | Path | None) -> Path:
    """Resolves output destination file ensuring telemetry volume is never touched.
    
    CRITICAL: Writes ONLY to a loadgen output volume/path, never to the telemetry volume.
    """
    if output_path:
        target = Path(output_path).resolve()
    else:
        env_val = os.getenv("LOADGEN_OUTPUT_PATH")
        if env_val:
            target = Path(env_val).resolve()
        else:
            # If in container with /loadgen mounted, write there
            container_vol = Path("/loadgen")
            if container_vol.is_dir():
                target = (container_vol / "summary.json").resolve()
            else:
                target = (Path.cwd() / "loadgen_output" / "summary.json").resolve()

    # Security check: never allow writing into telemetry paths
    for part in target.parts:
        if part.lower() == "telemetry":
            raise ValueError(
                f"Forbidden destination: Loadgen summary must never be written "
                f"to the telemetry volume ({target})."
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


async def execute_load_run(
    profile_name_or_path: str,
    duration_s: float,
    seed: int | None = None,
    gateway_url: str | None = None,
    output_path: str | Path | None = None,
    profiles_file: str | Path | None = None,
) -> RunSummary:
    """Executes a complete open-loop load generation run and persists summary."""
    profile: LoadProfile = get_profile(profile_name_or_path, profiles_file)
    effective_seed = profile.seed if seed is None else seed

    gw_url: str = gateway_url or os.getenv("GATEWAY_URL") or "http://127.0.0.1:8080"
    dest_path = resolve_output_path(output_path)

    # 1. Generate deterministic open-loop schedule
    requests = generate_schedule(profile, duration_s=duration_s, seed=effective_seed)
    seq_hash = hash_request_sequence(requests)

    print("=" * 80)
    print(f"OpsPilot LoadGen: Starting profile '{profile.profile_id}'")
    print(f"Target Gateway:    {gw_url}")
    print(f"Profile Type:      {profile.type.value}")
    print(f"Duration:          {duration_s}s")
    print(f"Seed:              {effective_seed}")
    print(f"Scheduled Reqs:    {len(requests)}")
    print(f"Schedule Hash:     {seq_hash[:16]}...")
    print(f"Output Path:       {dest_path}")
    print("=" * 80)

    # 2. Instantiate and attach scheduler
    scheduler = OpenLoopScheduler(
        profile=profile,
        requests=requests,
        duration_s=duration_s,
        gateway_url=gw_url,
    )

    loop = asyncio.get_running_loop()
    # Install signal handlers where supported (e.g. Unix / WSL)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, scheduler.stop)
    except (NotImplementedError, AttributeError):
        pass

    summary = await scheduler.run()
    summary.metadata["sequence_hash"] = seq_hash
    summary.metadata["gateway_url"] = gw_url

    # 3. Write summary JSON to destination
    with open(dest_path, "w", encoding="utf-8") as f:
        json.dump(summary.model_dump(), f, indent=2)

    print("\n" + "=" * 80)
    print("                    LOADGEN RUN SUMMARY                    ")
    print("=" * 80)
    print(f"Profile:        {summary.profile_id} (seed={summary.seed})")
    print(f"Duration:       {summary.duration_s:.2f}s")
    print(f"Target RPS:     {summary.target_rps:.2f}")
    print(f"Achieved RPS:   {summary.achieved_rps:.2f}")
    print(f"Total Requests: {summary.total_requests}")
    print(f"Error Count:    {summary.error_count} ({summary.error_rate * 100:.2f}%)")
    print(f"Status Counts:  {summary.status_counts}")
    if summary.latencies_ms:
        print(
            f"Latency (ms):   avg={summary.latencies_ms.avg} | "
            f"p50={summary.latencies_ms.p50} | "
            f"p95={summary.latencies_ms.p95} | "
            f"p99={summary.latencies_ms.p99}"
        )
    print(f"Summary saved:  {dest_path}")
    print("=" * 80)

    return summary
