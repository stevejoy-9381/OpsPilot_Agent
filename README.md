# OpsPilot

### Problem Statement
Modern cloud-native systems face complex production incidents where critical diagnostic context is scattered across disparate logs, metrics, and traces. Autonomous agents powered by large language models offer rapid investigation, but granting them unrestricted shell access or cloud credentials risks catastrophic failures, hallucinations, and unsafe system mutations. OpsPilot resolves this dilemma by enforcing deterministic safety boundaries: the LLM only investigates and proposes structured recovery plans, while policy authorization, exact-plan human approval, narrow execution, and outcome verification are strictly governed by deterministic code outside the model.

---


### Architecture at a Glance

```
                +-------------------------------------------------+
                |                   Review UI                     |
                |               (React + Vite + TS)               |
                +-----------------------+-------------------------+
                                        | HTTP (Port 5173 -> 8000)
                                        v
+---------------------------------------+---------------------------------------+
|  CORE (Network: core)                 |                                       |
|                                       v                                       |
|                              +-----------------+                              |
|                              |   FastAPI App   |                              |
|                              |   (apps/api)    |                              |
|                              +--------+--------+                              |
|                                       |                                       |
|           +---------------------------+---------------------------+           |
|           v                                                       v           |
|   +---------------+                                       +---------------+   |
|   |  PostgreSQL   | <--- Alembic Migrations               |     Redis     |   |
|   |   (Port 5432) |                                       |  (Port 6379)  |   |
|   +---------------+                                       +---------------+   |
|           ^                                                       ^           |
|           | Checkpoints & State                                   | Cache     |
|   +-------+-------+                                               |           |
|   |   LangGraph   |                                               |           |
|   |  Agent Core   |                                               |           |
|   +-------+-------+                                               |           |
|           |                                                       |           |
|           v                                                       |           |
|   +---------------+                                               |           |
|   |  LLM Adapter  | (GeminiClient / FakeLLMClient)                |           |
|   |  (agent/llm)  | Isolated provider SDK boundary                |           |
|   +---------------+                                               |           |
|                                                                               |
+-------------------------------------------------------------------------------+
                                        | (Proposals via Database/API)
                                        v
+-------------------------------------------------------------------------------+
|  BROKER (Network: broker only)                                                |
|                                                                               |
|   +---------------+  Approved Action Only  +--------------------------------+ |
|   | Action Broker | ---------------------> | Toy Lab Services & Fault Inject| |
|   | (broker/)     |                        | (lab/ - Feature 1)             | |
|   +---------------+                        +--------------------------------+ |
|   * Docker socket mounted ONLY here                                           |
+-------------------------------------------------------------------------------+
```

---

### Quickstart

#### Prerequisites
- Docker & Docker Compose v2+
- Python 3.12+ and `uv`
- Node.js 22+ and `npm`

#### 1. Setup Environment
```bash
cp .env.example .env
```

#### 2. Install Dependencies
```bash
# Backend toolchain (uv)
make install

# Web UI dependencies (npm)
make web-install
```

#### 3. Start Infrastructure, Migrations, and API
```bash
make up
```
This command starts PostgreSQL 17 and Redis 7 in Docker, waits for PostgreSQL to become healthy, applies Alembic database migrations (`uv run alembic upgrade head`), and starts the FastAPI service at `http://127.0.0.1:8000`.

#### 4. Verify Readiness
```bash
curl http://127.0.0.1:8000/readyz
# {"status":"ok","dependencies":{"postgres":"ok","redis":"ok"}}
```

#### 5. Launch the Review UI (in another terminal)
```bash
make web-dev
# Open http://localhost:5173 in your browser
```

#### 6. Stop and Clean Up
```bash
# Stop containers and preserve volumes
make down

# Wipe data volumes (requires interactive confirmation)
make reset
```

---

### Development Commands

| Target | Command | Purpose |
| :--- | :--- | :--- |
| `make install` | `uv sync --extra dev` | Install pinned backend Python toolchain |
| `make lint` | `uv run ruff check .` | Run Ruff linter across Python code |
| `make typecheck` | `uv run mypy apps agent tests` | Run Mypy strict typechecking |
| `make test` | `uv run pytest -v` | Run full backend test suite offline |
| `make migrate` | `uv run alembic upgrade head` | Run Alembic database migrations |
| `make web-install`| `cd apps/web && npm install` | Install React frontend packages |
| `make web-lint` | `cd apps/web && npm run lint` | Run ESLint on frontend code |
| `make web-test` | `cd apps/web && npm test` | Run Vitest unit tests |
| `make up` | Docker + Alembic + API | Spin up full local development stack |
| `make down` | Docker Compose down | Gracefully stop local containers |
| `make reset` | Docker Compose down -v | Wipe persistent volumes with prompt |

---

### Status & Backlog

- [x] **Feature 0: Foundation Complete**
  - Monorepo directory skeleton and Docker socket boundary guard
  - Docker Compose core services (PostgreSQL 17, Redis 7) bound to `127.0.0.1`
  - FastAPI backend with structured JSON logging, sensitive data redaction, `/healthz`, and `/readyz`
  - React + Vite + TypeScript review UI scaffold with dependency readiness dashboard
  - Outcome contract and deterministic state machine specification ([docs/outcome_contract.md](docs/outcome_contract.md))
  - Architectural decisions log ([docs/DECISIONS.md](docs/DECISIONS.md))
  - Provider-isolated LLM adapter with Google GenAI SDK, offline deterministic replay client, retry/backoff, and token budget guard
  - Alembic baseline migrations wired to `DATABASE_URL`
  - GitHub Actions CI workflow and pre-commit secret scanning hooks
- [x] **Feature 1: Synthetic Microservice Lab & Evaluation Substrate Complete**
  - Toy microservices (`gateway`, `orders`, `inventory`) with W3C trace propagation, connection pool limits, and telemetry
  - Telemetry collection: Prometheus scraping (5s), OpenTelemetry collector exporting JSONL spans, and correlated structured JSON logs
  - In-process deploy and rollback primitives (`lab.controller_lib`) with version profiles (`v1.0.0`-`v1.3.0`) and non-incriminating diffs
  - Open-loop load generator (`lab.loadgen`) with deterministic Zipf product distributions and named YAML profiles
  - Segregated scenario controller on `lab-admin` and isolated SQLite Ground-Truth Vault (`vault.db`) with token protection
  - Formal scenario schema and 5 scenario definitions (`S1`-`S5`) covering all failure families
  - Deterministic evaluation graders (`evals/graders/`) for diagnosis, evidence support, recovery, and safety
  - Zero-leakage evidence exporter (`scripts/export_evidence.py`) and leak test suite
  - Blind diagnosis kit (`scripts/blind_kit.py`), verified human diagnosis log, and evidence requirements contract ([docs/evidence_requirements.md](docs/evidence_requirements.md))
  - Full lab architecture, version profiles, and failure signature report ([docs/LAB_REPORT.md](docs/LAB_REPORT.md))
  - End-to-end scenario lifecycle test suite (`tests/lab_e2e`) with fast CI and nightly workflows
  - Lab management targets: `make lab-up`, `make lab-down`, `make lab-smoke`
- [ ] **Feature 2: Evidence Gathering & Diagnosis Agent** (LangGraph workflow nodes, bounded read tools)
- [ ] **Feature 3: Exact-Plan Review UI & Execution Broker** (Cryptographic plan hashing, human approval gate)
- [ ] **Feature 4: Independent Verification & Escalation Engine** (Outcome proof, rollback on degradation)
