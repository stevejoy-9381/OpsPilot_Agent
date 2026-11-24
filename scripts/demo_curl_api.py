"""Live demonstration script for OpsPilot REST API.

Generates realistic curl transcripts for:
1. POST /incidents (create incident from seeded case or alert)
2. POST /runs (create run with required Idempotency-Key)
3. GET /runs/{id} (get run detail with status, version, remaining_calls, hypotheses, etc.)
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from apps.api.main import app


async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        # 1. Login as reviewer
        print("# ============================================================================")
        print("# 0. Authenticate as Reviewer (Session cookie + CSRF token)")
        print("# ============================================================================")
        print("curl -i -X POST http://localhost:8000/auth/login \\")
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"email": "bob.reviewer@example.com", "password": "ReviewerPass123!"}\'')
        print()

        login_resp = await client.post(
            "/auth/login",
            json={"email": "bob.reviewer@example.com", "password": "ReviewerPass123!"},
        )
        print(f"HTTP/1.1 {login_resp.status_code} OK")
        for k, v in login_resp.headers.items():
            if k.lower() in ("set-cookie", "content-type"):
                print(f"{k}: {v}")
        print()
        print(json.dumps(login_resp.json(), indent=2))
        print("\n" + "=" * 80 + "\n")

        csrf_token = login_resp.json()["csrf_token"]
        headers = {
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
        }

        # 2. POST /incidents: Create incident from seeded benchmark case S1
        print("# ============================================================================")
        print("# 1. POST /incidents: Create incident from seeded benchmark case S1")
        print("# ============================================================================")
        inc_idemp_key = f"inc-demo-{uuid.uuid4().hex[:8]}"
        print(f"curl -i -X POST http://localhost:8000/incidents \\")
        print("    -b opspilot_session=<reviewer_cookie> \\")
        print(f'    -H "X-CSRF-Token: {csrf_token}" \\')
        print(f'    -H "Idempotency-Key: {inc_idemp_key}" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"case_id": "S1"}\'')
        print()

        inc_resp = await client.post(
            "/incidents",
            json={"case_id": "S1"},
            headers={**headers, "Idempotency-Key": inc_idemp_key},
        )
        print(f"HTTP/1.1 {inc_resp.status_code} Created")
        for k, v in inc_resp.headers.items():
            if k.lower() in ("content-type",):
                print(f"{k}: {v}")
        print()
        inc_data = inc_resp.json()
        print(json.dumps(inc_data, indent=2))
        incident_id = inc_data["id"]
        print("\n" + "=" * 80 + "\n")

        # 3. POST /runs: Create run with required Idempotency-Key
        print("# ============================================================================")
        print("# 2. POST /runs: Create investigation run on incident (status: queued)")
        print("# ============================================================================")
        run_idemp_key = f"run-demo-{uuid.uuid4().hex[:8]}"
        print(f"curl -i -X POST http://localhost:8000/runs \\")
        print("    -b opspilot_session=<reviewer_cookie> \\")
        print(f'    -H "X-CSRF-Token: {csrf_token}" \\')
        print(f'    -H "Idempotency-Key: {run_idemp_key}" \\')
        print('    -H "Content-Type: application/json" \\')
        print(f'    -d \'{{"incident_id": "{incident_id}", "max_calls": 8}}\'')
        print()

        run_resp = await client.post(
            "/runs",
            json={"incident_id": incident_id, "max_calls": 8},
            headers={**headers, "Idempotency-Key": run_idemp_key},
        )
        print(f"HTTP/1.1 {run_resp.status_code} Created")
        for k, v in run_resp.headers.items():
            if k.lower() in ("content-type",):
                print(f"{k}: {v}")
        print()
        run_data = run_resp.json()
        print(json.dumps(run_data, indent=2))
        run_id = run_data["id"]
        print("\n" + "=" * 80 + "\n")

        # 4. GET /runs/{id}: Retrieve run detail
        print("# ============================================================================")
        print("# 3. GET /runs/{id}: Retrieve detailed run state and aggregates")
        print("# ============================================================================")
        print(f"curl -i -X GET http://localhost:8000/runs/{run_id} \\")
        print("    -b opspilot_session=<reviewer_cookie>")
        print()

        get_resp = await client.get(f"/runs/{run_id}")
        print(f"HTTP/1.1 {get_resp.status_code} OK")
        for k, v in get_resp.headers.items():
            if k.lower() in ("content-type",):
                print(f"{k}: {v}")
        print()
        print(json.dumps(get_resp.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
