from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from lab.loadgen.models import (
    LatencyStats,
    LoadProfile,
    RunSummary,
    ScheduledRequest,
    SteadyProfile,
)


class OpenLoopScheduler:
    """Open-loop load scheduler that dispatches requests strictly on schedule.
    
    Requests fire at their designated timestamps regardless of upstream response times,
    preventing latency regressions from masking throughput issues.
    """

    def __init__(
        self,
        profile: LoadProfile,
        requests: list[ScheduledRequest],
        duration_s: float,
        gateway_url: str = "http://127.0.0.1:8080",
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int = 500,
    ) -> None:
        self.profile = profile
        self.requests = requests
        self.duration_s = duration_s
        self.gateway_url = gateway_url.rstrip("/")
        self.http_client = http_client
        self.max_concurrency = max_concurrency

        self._stop_event = asyncio.Event()
        self._latencies_ms: list[float] = []
        self._status_counts: dict[str, int] = {}
        self._dispatch_times: list[float] = []
        self._errors: list[str] = []
        self._dispatched_count = 0

    def stop(self) -> None:
        """Signals the scheduler to stop dispatching new requests."""
        self._stop_event.set()

    async def _dispatch_single_request(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        req: ScheduledRequest,
    ) -> None:
        """Dispatches one checkout request and records outcome."""
        async with sem:
            payload = {"product_id": req.product_id, "qty": req.qty}
            t0 = time.perf_counter()
            status_str = "error"
            try:
                resp = await client.post(
                    f"{self.gateway_url}/checkout",
                    json=payload,
                    timeout=10.0,
                )
                dur_ms = (time.perf_counter() - t0) * 1000.0
                status_str = str(resp.status_code)
                self._latencies_ms.append(dur_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                status_str = "error"
                self._errors.append(str(exc))
                self._latencies_ms.append(dur_ms)
            finally:
                self._status_counts[status_str] = self._status_counts.get(status_str, 0) + 1

    async def run(self) -> RunSummary:
        """Executes the open-loop schedule until duration completes or stop is signaled."""
        start_utc = datetime.now(timezone.utc).isoformat()
        mono_start = time.monotonic()
        sem = asyncio.Semaphore(self.max_concurrency)
        active_tasks: set[asyncio.Task[Any]] = set()

        close_client = False
        if self.http_client is not None:
            client = self.http_client
        else:
            client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=self.max_concurrency,
                    max_keepalive_connections=100,
                ),
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            close_client = True

        try:
            for req in self.requests:
                if self._stop_event.is_set():
                    break

                target_mono = mono_start + req.offset_s
                now = time.monotonic()
                sleep_s = target_mono - now

                if sleep_s > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
                        if self._stop_event.is_set():
                            break
                    except asyncio.TimeoutError:
                        pass

                # Dispatch open-loop task without awaiting response
                self._dispatch_times.append(time.monotonic() - mono_start)
                self._dispatched_count += 1
                task = asyncio.create_task(
                    self._dispatch_single_request(client, sem, req)
                )
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)

            # Wait for in-flight tasks to complete (with a 3s drain timeout)
            if active_tasks:
                done, pending = await asyncio.wait(active_tasks, timeout=3.0)
                for p in pending:
                    p.cancel()

        finally:
            if close_client:
                await client.aclose()

        mono_end = time.monotonic()
        end_utc = datetime.now(timezone.utc).isoformat()
        elapsed_s = max(0.001, mono_end - mono_start)

        # Calculate latency statistics
        lat_stats = None
        if self._latencies_ms:
            sorted_lat = sorted(self._latencies_ms)
            n = len(sorted_lat)

            def percentile(p: float) -> float:
                k = (n - 1) * p
                f = math.floor(k)
                c = math.ceil(k)
                if f == c:
                    return sorted_lat[int(k)]
                d0 = sorted_lat[int(f)] * (c - k)
                d1 = sorted_lat[int(c)] * (k - f)
                return d0 + d1

            lat_stats = LatencyStats(
                min=round(sorted_lat[0], 2),
                avg=round(sum(sorted_lat) / n, 2),
                p50=round(percentile(0.50), 2),
                p95=round(percentile(0.95), 2),
                p99=round(percentile(0.99), 2),
                max=round(sorted_lat[-1], 2),
            )

        total_reqs = sum(self._status_counts.values())
        achieved_rps = round(total_reqs / elapsed_s, 2)

        # Target RPS
        if isinstance(self.profile, SteadyProfile):
            target_rps = self.profile.rps
        else:
            target_rps = (
                round(len(self.requests) / self.duration_s, 2)
                if self.duration_s > 0
                else 0.0
            )

        # Count 5xx, timeouts, network errors
        error_count = sum(
            cnt
            for st, cnt in self._status_counts.items()
            if st.startswith("5") or st == "error"
        )
        error_rate = round(error_count / total_reqs, 4) if total_reqs > 0 else 0.0

        return RunSummary(
            profile_id=self.profile.profile_id,
            seed=self.profile.seed,
            duration_s=round(elapsed_s, 2),
            start_time=start_utc,
            end_time=end_utc,
            target_rps=target_rps,
            achieved_rps=achieved_rps,
            total_requests=total_reqs,
            status_counts=self._status_counts,
            error_count=error_count,
            error_rate=error_rate,
            latencies_ms=lat_stats,
            metadata={
                "scheduled_count": len(self.requests),
                "dispatched_count": self._dispatched_count,
            },
        )
