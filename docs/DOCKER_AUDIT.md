# Docker Audit Report

This audit tracks all Docker-dependent integration, validation, and container runtime checks across OpsPilot and the synthetic lab environment.

> [!NOTE]
> Per project constraints, Docker is not run in local restricted environments. Every check below is marked **"not run - needs Codespaces"** alongside the exact commands to execute in a GitHub Codespaces container (using `.devcontainer/devcontainer.json` with the `docker-in-docker` feature).

---

## Audit Checklist & Codespaces Verification Runbook

| Check / Verification Target | Status | Target Environment | Exact Command to Run in Codespaces | Expected Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **1. Compose Configuration Validation** | `not run - needs Codespaces` | GitHub Codespaces | `docker compose --env-file .env -f infra/docker-compose.yml config && docker compose --env-file .env -f infra/docker-compose.lab.yml config` | Clean YAML output, exit code 0 |
| **2. Docker Socket Absence Enforcement** | `not run - needs Codespaces` | GitHub Codespaces | `bash scripts/check_no_docker_socket.sh` | `check_no_docker_socket: OK (0 references in 2 Compose file(s))` |
| **3. Core Stack Startup & Readiness** | `not run - needs Codespaces` | GitHub Codespaces | `make up` | Starts `postgres` and `redis` (`core` profile), runs Alembic migrations, passes `GET /readyz` |
| **4. Lab Stack Build & Startup** | `not run - needs Codespaces` | GitHub Codespaces | `make lab-up` | Builds `gateway`, `orders`, `inventory`, brings up `lab-postgres` & `lab-redis`, seeds DB |
| **5. Lab Microservices Smoke Test** | `not run - needs Codespaces` | GitHub Codespaces | `make lab-smoke` | 50 checkouts sent to `http://127.0.0.1:8080/checkout`, >95% return 200/409 |
| **6. Lean Lab Startup Without Rebuild** | `not run - needs Codespaces` | GitHub Codespaces | `make lab-lean` | Brings up existing lab containers using `--profile lab` |
| **7. Core Network Isolation Proof** | `not run - needs Codespaces` | GitHub Codespaces | `docker run --rm --network opspilot_core curlimages/curl:latest http://orders:8000/admin/state` | Connection fails / unresolvable (admin unreachable from `core`) |
| **8. Memory Footprint Verification** | `not run - needs Codespaces` | GitHub Codespaces | `docker stats --no-stream` | Aggregate memory consumption across `core` + `lab` containers strictly < 4.0 GB (target: ~3.2 GB) |
| **9. Clean Stack Teardown** | `not run - needs Codespaces` | GitHub Codespaces | `make lab-down && make down` | All containers cleanly stopped and removed |

---

## Codespaces Quickstart Instructions

When launching the repository inside a GitHub Codespace:

```bash
# 1. Setup toolchains and dependencies
make codespace-setup

# 2. Verify static analysis and offline test suite
make lint typecheck test

# 3. Start and verify synthetic lab
make lab-up
make lab-smoke

# 4. Verify network boundary isolation from core
docker run --rm --network opspilot_core curlimages/curl:latest http://orders:8000/admin/state || echo "Verified: unreachable from core"
```
