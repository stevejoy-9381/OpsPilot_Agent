# Docker Audit Report

This audit tracks all Docker-dependent integration, validation, and container runtime checks across OpsPilot and the synthetic lab environment.

> [!CAUTION]
> **ENVIRONMENT POLICY**: Docker is NOT installed on this machine and must NOT be run, installed, or backgrounded as a daemon.
> Every Docker-dependent check below is marked **"NOT RUN - needs Codespaces"**.
> All verification requiring container networks, containerized PostgreSQL/Redis, live Prometheus scraping, or Docker daemon execution must only be performed inside a GitHub Codespaces container (using `.devcontainer/devcontainer.json` with the `docker-in-docker` feature).

---

## Audit Checklist: Docker-Dependent Targets

| Check / Verification Target | Status | Target Environment | Exact Command to Run in Codespaces | Expected Outcome |
| :--- | :--- | :--- | :--- | :--- |
| **1. Compose Configuration Validation** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `docker compose --env-file .env -f infra/docker-compose.yml config && docker compose --env-file .env -f infra/docker-compose.lab.yml config` | Clean YAML validation output, exit code 0 |
| **2. Docker Socket Absence Enforcement** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `bash scripts/check_no_docker_socket.sh` | `check_no_docker_socket: OK (0 references in 2 Compose file(s))` |
| **3. Core Stack Startup & Readiness** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `make up` | Starts `postgres` and `redis` (`core` profile), runs Alembic migrations, passes `GET /readyz` |
| **4. Lab Stack Build & Startup** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `make lab-up` | Builds `gateway`, `orders`, `inventory`, brings up `lab-postgres` & `lab-redis`, seeds DB |
| **5. Lab Microservices Smoke Test** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `make lab-smoke` | 50 checkouts sent to `http://127.0.0.1:8080/checkout`, >95% return 200/409 |
| **6. Lean Lab Startup Without Rebuild** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `make lab-lean` | Brings up existing lab containers using `--profile lab` |
| **7. Core Network Isolation Proof** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `docker run --rm --network opspilot_core curlimages/curl:latest http://orders:8000/admin/state` | Connection fails / unresolvable (admin unreachable from `core`) |
| **8. Memory Footprint Verification** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `docker stats --no-stream` | Aggregate memory consumption across `core` + `lab` containers strictly < 4.0 GB (target: ~3.2 GB) |
| **9. Clean Stack Teardown** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `make lab-down && make down` | All containers cleanly stopped and removed |
| **10. Live Telemetry Ingestion (Prometheus/OTel)** | `NOT RUN - needs Codespaces` | GitHub Codespaces | Prometheus scrape check on `:9090` and OTel file sink verification | Verified active metric scrapes and span ingestion from live services |
| **11. Live Evidence Export (`export_evidence.py`)** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `uv run python scripts/export_evidence.py <run_id> --start ... --end ...` against running stack | Export directory populated directly from live Prometheus and telemetry volume |
| **12. Live Scenario E2E Tests (`pytest -m lab`)** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `uv run pytest -v -m lab` against live Docker containers | S1–S5 inject -> breach -> rollback -> recovery verified on live container processes |
| **13. Real Network Leakage & Vault Isolation Test** | `NOT RUN - needs Codespaces` | GitHub Codespaces | `uv run pytest -k test_core_network_isolation_live_docker` | Live container on `core` network rejected from accessing vault volume and admin APIs |
| **14. Dedicated Evidence Compose Network & Tool Isolation** | `NOT RUN - needs Codespaces` | GitHub Codespaces | Dedicated `evidence` Compose network; mount telemetry volume `:ro`; probe controller, vault, lab /admin, loadgen output | Tool process has read-only access to Prometheus and telemetry volume; controller, vault, /admin routes, and loadgen are strictly unreachable |

---

## Synthetic vs Real Lab Asset Inventory

To prevent confusion between offline mocked development assets and real runtime output, all assets on this machine are cataloged below:

| Asset Path | Origin Classification | Description |
| :--- | :--- | :--- |
| `fixtures/scenarios/*.yml` | `SYNTHETIC-HANDWRITTEN` | Hand-authored scenario definitions describing S1–S5 incident physics and expected actions. |
| `blind_packets/*` | `SYNTHETIC-HANDWRITTEN` | Programmatically generated offline mock telemetry and audit files produced by `scripts/blind_kit.py` for blind human diagnosis evaluation. Never captured from a live Docker cluster. |
| `docs/human_diagnosis_log.md` | `SYNTHETIC-HANDWRITTEN` | Diagnosis log and confidence scores evaluated against `SYNTHETIC-HANDWRITTEN` blind packets. |
| `docs/evidence_requirements.md` | `SYNTHETIC-HANDWRITTEN` | Evidence signal requirements derived from the offline blind diagnosis experiment. |
| `tests/lab_e2e/test_scenarios_e2e.py` | `MOCKED` | Uses `AsyncMock`, `patch`, and mocked PromQL endpoints to verify controller state-machine logic in pure Python without Docker. |
| `tests/test_leakage.py` (offline tests) | `MOCKED` | Validates keyword scanning and sanitization logic over `tmp_path` fixture files. |

---

## Codespaces Quickstart Instructions

When launching the repository inside a GitHub Codespace with Docker-in-Docker enabled:

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

# 5. Run full live Docker lab scenario lifecycle suite
uv run pytest -v -m lab
```
