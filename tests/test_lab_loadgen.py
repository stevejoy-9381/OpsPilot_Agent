from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from lab.loadgen.cli import main as cli_main
from lab.loadgen.generator import (
    ZipfSampler,
    generate_schedule,
    hash_request_sequence,
)
from lab.loadgen.models import (
    BurstProfile,
    RampProfile,
    SteadyProfile,
)
from lab.loadgen.runner import resolve_output_path
from lab.loadgen.scheduler import OpenLoopScheduler


def test_zipf_distribution_characteristics() -> None:
    """Verifies that ZipfSampler produces realistic skewed catalog product distributions."""
    import random

    sampler = ZipfSampler(n=1000, s=1.2)
    rng = random.Random(42)

    samples = [sampler.sample(rng) for _ in range(5000)]
    assert all(1 <= s <= 1000 for s in samples)

    # In Zipf(1.2), item 1 must appear far more frequently than tail items
    count_1 = samples.count(1)
    count_1000 = samples.count(1000)
    assert count_1 > 500, f"Expected item 1 count > 500, got {count_1}"
    assert count_1 > count_1000 * 20, (
        f"Item 1 count ({count_1}) must dominate tail item 1000 ({count_1000})"
    )


def test_sequence_determinism_same_seed_identical_hash() -> None:
    """Verifies that the same profile + seed yields an identical request list and SHA-256 hash."""
    profile = SteadyProfile(profile_id="test_steady", rps=20.0, seed=7)
    duration = 10.0

    # Run 1
    reqs_1 = generate_schedule(profile, duration_s=duration, seed=7)
    hash_1 = hash_request_sequence(reqs_1)

    # Run 2 with identical seed
    reqs_2 = generate_schedule(profile, duration_s=duration, seed=7)
    hash_2 = hash_request_sequence(reqs_2)

    assert hash_1 == hash_2
    assert len(reqs_1) == len(reqs_2) == 200
    for r1, r2 in zip(reqs_1, reqs_2, strict=False):
        assert r1.offset_s == r2.offset_s
        assert r1.product_id == r2.product_id
        assert r1.qty == r2.qty

    # Run 3 with different seed
    reqs_3 = generate_schedule(profile, duration_s=duration, seed=99)
    hash_3 = hash_request_sequence(reqs_3)
    assert hash_1 != hash_3


def test_burst_and_ramp_profiles_determinism() -> None:
    """Verifies deterministic sequence generation for burst and ramp profiles."""
    burst_prof = BurstProfile(
        profile_id="test_burst",
        baseline_rps=10.0,
        spike_multiplier=3.0,
        spike_duration_s=2.0,
        spike_start_s=2.0,
        seed=42,
    )
    b_reqs_1 = generate_schedule(burst_prof, duration_s=6.0, seed=42)
    b_reqs_2 = generate_schedule(burst_prof, duration_s=6.0, seed=42)
    assert hash_request_sequence(b_reqs_1) == hash_request_sequence(b_reqs_2)

    ramp_prof = RampProfile(
        profile_id="test_ramp",
        start_rps=5.0,
        end_rps=25.0,
        seed=42,
    )
    r_reqs_1 = generate_schedule(ramp_prof, duration_s=4.0, seed=42)
    r_reqs_2 = generate_schedule(ramp_prof, duration_s=4.0, seed=42)
    assert hash_request_sequence(r_reqs_1) == hash_request_sequence(r_reqs_2)


@pytest.mark.asyncio
async def test_schedule_accuracy_open_loop_within_tolerance() -> None:
    """Verifies open-loop schedule accuracy; server latency does not mask dispatch times."""
    profile = SteadyProfile(profile_id="steady_acc", rps=20.0, seed=123)
    duration_s = 2.0  # 40 requests
    requests = generate_schedule(profile, duration_s=duration_s, seed=123)

    dispatched_timestamps: list[float] = []

    # Mock handler that simulates 30ms server latency
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        dispatched_timestamps.append(time.monotonic())
        await asyncio.sleep(0.03)  # 30ms latency
        return httpx.Response(
            201, json={"checkout_id": "chk-123", "status": "CONFIRMED", "order": {}}
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        scheduler = OpenLoopScheduler(
            profile=profile,
            requests=requests,
            duration_s=duration_s,
            gateway_url="http://gateway",
            http_client=client,
        )
        summary = await scheduler.run()

    # Even with 30ms server response latency, all 40 requests must be dispatched
    assert summary.total_requests == 40
    assert summary.status_counts.get("201") == 40

    # Achieved RPS should be within tolerance of target 20.0 rps
    assert 16.0 <= summary.achieved_rps <= 24.0, (
        f"Achieved RPS {summary.achieved_rps} out of expected tolerance [16, 24]"
    )


@pytest.mark.asyncio
async def test_stop_signal_terminates_gracefully() -> None:
    """Verifies that stop() halts execution promptly and writes a partial run summary."""
    profile = SteadyProfile(profile_id="steady_stop", rps=20.0, seed=456)
    duration_s = 10.0  # Would be 200 requests
    requests = generate_schedule(profile, duration_s=duration_s, seed=456)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "CONFIRMED"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        scheduler = OpenLoopScheduler(
            profile=profile,
            requests=requests,
            duration_s=duration_s,
            gateway_url="http://gateway",
            http_client=client,
        )

        async def trigger_stop_after_delay() -> None:
            await asyncio.sleep(0.5)
            scheduler.stop()

        stop_task = asyncio.create_task(trigger_stop_after_delay())
        t0 = time.monotonic()
        summary = await scheduler.run()
        elapsed = time.monotonic() - t0
        await stop_task

    # Execution must terminate well before full 10s duration
    assert elapsed < 3.0
    # Dispatched requests should be around 0.5s * 20rps = ~10 requests (far less than 200)
    assert 5 <= summary.total_requests <= 30
    assert summary.duration_s < 3.0


def test_resolve_output_path_forbids_telemetry_volume(tmp_path: Path) -> None:
    """Verifies security invariant: Loadgen summary cannot be written to telemetry volume."""
    telemetry_dest = tmp_path / "telemetry" / "summary.json"
    with pytest.raises(ValueError, match="never be written to the telemetry volume"):
        resolve_output_path(telemetry_dest)

    # Valid output path should succeed
    valid_dest = tmp_path / "loadgen" / "summary.json"
    resolved = resolve_output_path(valid_dest)
    assert resolved == valid_dest.resolve()


def test_cli_generate_and_list_profiles(capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies CLI list-profiles and generate dry-run subcommands."""
    # list-profiles
    rc = cli_main(["list-profiles"])
    assert rc == 0
    out_list = capsys.readouterr().out
    assert "steady_20rps" in out_list
    assert "burst_5x" in out_list
    assert "ramp_load" in out_list

    # generate
    rc = cli_main(["generate", "--profile", "steady_20rps", "--seed", "7", "--duration", "5"])
    assert rc == 0
    out_gen = capsys.readouterr().out
    assert "Requests:      100" in out_gen
    assert "Sequence Hash:" in out_gen
