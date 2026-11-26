"""Live demonstration script for OpsPilot Authentication and RBAC.

Simulates HTTP requests with curl output format against the FastAPI application.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from apps.api.main import app


async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        # 1. Login as Viewer
        print(">>> curl -i -X POST http://localhost:8000/auth/login \\")
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"email": "alice.viewer@example.com", "password": "ViewerPass123!"}\'')
        print()

        login_resp = await client.post(
            "/auth/login",
            json={"email": "alice.viewer@example.com", "password": "ViewerPass123!"},
        )
        print(f"HTTP/1.1 {login_resp.status_code} OK")
        for k, v in login_resp.headers.items():
            if k.lower() in ("set-cookie", "content-type"):
                print(f"{k}: {v}")
        print()
        print(json.dumps(login_resp.json(), indent=2))
        print("\n" + "=" * 80 + "\n")

        # 2. Get /auth/me (Identity derived from session cookie)
        print(">>> curl -i -X GET http://localhost:8000/auth/me \\")
        print("    -b opspilot_session=<cookie>")
        print()

        me_resp = await client.get("/auth/me")
        print(f"HTTP/1.1 {me_resp.status_code} OK")
        for k, v in me_resp.headers.items():
            if k.lower() in ("content-type",):
                print(f"{k}: {v}")
        print()
        print(json.dumps(me_resp.json(), indent=2))
        print("\n" + "=" * 80 + "\n")

        # 3. Rejected cross-role call (Viewer calling Reviewer-only route)
        print(">>> curl -i -X POST http://localhost:8000/auth/review/approve \\")
        print("    -b opspilot_session=<viewer_cookie> \\")
        print('    -H "X-Requested-With: XMLHttpRequest" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"plan_id": "00000000-0000-0000-0000-000000000001", "decision": "approve"}\'')
        print()

        cross_resp = await client.post(
            "/auth/review/approve",
            json={
                "plan_id": "00000000-0000-0000-0000-000000000001",
                "decision": "approve",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        print(f"HTTP/1.1 {cross_resp.status_code} Forbidden")
        for k, v in cross_resp.headers.items():
            if k.lower() in ("content-type",):
                print(f"{k}: {v}")
        print()
        print(json.dumps(cross_resp.json(), indent=2))
        print("\n" + "=" * 80 + "\n")

        # 4. Rejected spoofed body call (attempting to supply tenant_id or reviewer_id in body by reviewer)
        print(">>> curl -i -X POST http://localhost:8000/auth/review/approve \\")
        print("    -b opspilot_session=<reviewer_cookie> \\")
        print('    -H "X-Requested-With: XMLHttpRequest" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"plan_id": "...", "decision": "approve", "tenant_id": "forged-uuid"}\'')
        print()

        # Login as reviewer first
        _ = await client.post(
            "/auth/login",
            json={"email": "bob.reviewer@example.com", "password": "ReviewerPass123!"},
        )
        spoofed_resp = await client.post(
            "/auth/review/approve",
            json={
                "plan_id": "00000000-0000-0000-0000-000000000001",
                "decision": "approve",
                "tenant_id": "00000000-0000-0000-0000-000000000099",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        print(f"HTTP/1.1 {spoofed_resp.status_code} Unprocessable Entity")
        print()
        print(json.dumps(spoofed_resp.json(), indent=2))
        print("\n" + "=" * 80 + "\n")

        # 5. Login as Reviewer and call /auth/review/approve successfully
        print(">>> curl -i -X POST http://localhost:8000/auth/login \\")
        print('    -d \'{"email": "bob.reviewer@example.com", "password": "ReviewerPass123!"}\'')
        print()
        rev_login = await client.post(
            "/auth/login",
            json={"email": "bob.reviewer@example.com", "password": "ReviewerPass123!"},
        )
        csrf_token = rev_login.json()["csrf_token"]
        print(f"HTTP/1.1 {rev_login.status_code} OK (Logged in as Reviewer)")
        print()

        print(">>> curl -i -X POST http://localhost:8000/auth/review/approve \\")
        print("    -b opspilot_session=<reviewer_cookie> \\")
        print(f'    -H "X-CSRF-Token: {csrf_token}" \\')
        print('    -H "Content-Type: application/json" \\')
        print('    -d \'{"plan_id": "00000000-0000-0000-0000-000000000001", "decision": "approve", "reason": "Verified"}\'')
        print()

        rev_approve = await client.post(
            "/auth/review/approve",
            json={
                "plan_id": "00000000-0000-0000-0000-000000000001",
                "decision": "approve",
                "reason": "Verified",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        print(f"HTTP/1.1 {rev_approve.status_code} OK")
        print()
        print(json.dumps(rev_approve.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
