"""Lab smoke test script: waits for health, sends 50 checkouts, and asserts health."""

from __future__ import annotations

import sys
import time

import httpx


def wait_for_gateway_health(
    gateway_url: str = "http://127.0.0.1:8080",
    timeout_s: float = 40.0,
) -> bool:
    """Waits until the gateway service responds with HTTP 200 on /healthz."""
    print(f"Waiting for Gateway at {gateway_url}/healthz...")
    start = time.perf_counter()
    while time.perf_counter() - start < timeout_s:
        try:
            resp = httpx.get(f"{gateway_url}/healthz", timeout=2.0)
            if resp.status_code == 200:
                print(f"Gateway is healthy: {resp.json()}")
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def run_smoke_test(gateway_url: str = "http://127.0.0.1:8080", count: int = 50) -> bool:
    """Sends checkouts to gateway and verifies >= 95% return 2xx or 409."""
    if not wait_for_gateway_health(gateway_url):
        print("ERROR: Gateway failed to become healthy within timeout.", file=sys.stderr)
        return False

    print(f"\nSending {count} checkout requests to {gateway_url}/checkout...")
    statuses: dict[int | str, int] = {}
    latencies: list[float] = []

    client = httpx.Client(base_url=gateway_url, timeout=5.0)
    for i in range(1, count + 1):
        product_id = (i % 30) + 1
        qty = 1 if i % 10 != 0 else 600  # Occasional bulk request to naturally trigger 409
        t0 = time.perf_counter()
        try:
            res = client.post("/checkout", json={"product_id": product_id, "qty": qty})
            dur = (time.perf_counter() - t0) * 1000
            latencies.append(dur)
            status = res.status_code
            statuses[status] = statuses.get(status, 0) + 1
        except Exception as exc:
            statuses["error"] = statuses.get("error", 0) + 1
            print(f"  Req #{i:02d} failed: {exc}")

    client.close()

    success_and_conflict = statuses.get(200, 0) + statuses.get(201, 0) + statuses.get(409, 0)
    ratio = success_and_conflict / count
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    err_count = statuses.get(502, 0) + statuses.get(504, 0) + statuses.get("error", 0)

    print("\n=======================================================")
    print("                Lab Smoke Test Summary                 ")
    print("=======================================================")
    print(f"Total Requests:       {count}")
    print(f"200 OK Checkouts:     {statuses.get(200, 0)}")
    print(f"409 Conflicts:        {statuses.get(409, 0)}")
    print(f"Errors (5xx/Fail):    {err_count}")
    print(f"Valid Rate (2xx/409): {ratio * 100:.1f}% (Required: >95%)")
    print(f"Avg Latency:          {avg_latency:.1f}ms")
    print("=======================================================\n")

    if ratio >= 0.95:
        print("SMOKE TEST PASSED: Lab services operating normally.")
        return True
    else:
        print(
            f"SMOKE TEST FAILED: Valid response rate {ratio*100:.1f}% below 95% threshold.",
            file=sys.stderr,
        )
        return False


if __name__ == "__main__":
    success = run_smoke_test()
    sys.exit(0 if success else 1)
