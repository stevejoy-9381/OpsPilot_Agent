# Foundation Report: OpsPilot Phase 0

## 1. Overview
The Foundation phase (Feature 0) establishes the complete reproducible toolchain, architectural boundaries, and safety contracts for OpsPilot. OpsPilot is an incident-investigation agent designed to diagnose and propose reversible recovery actions on local lab microservices while keeping LLM reasoning strictly segregated from execution authorization, broker mutation, and outcome verification.

---

## 2. What Was Built

### Phase 0.1: Monorepo Skeleton & Safety Boundaries
- **Directory Hierarchy**: Clean separation of `apps/` (api, web), `agent/` (graph, llm), `tools/` (read, actions), `broker/`, `fixtures/`, `evals/`, `lab/`, `infra/`, `docs/`, and `scripts/`.
- **Docker Socket Isolation Check**: [`scripts/check_no_docker_socket.sh`](../scripts/check_no_docker_socket.sh) ensures `/var/run/docker.sock` is never mounted in any compose service except an explicitly allowlisted broker service (none currently).
- **Environment Isolation**: `.env.example` committed with safe placeholders; `.env` strictly git-ignored.

### Phase 0.2: Docker Compose Infrastructure
- **Core Stack**: [`infra/docker-compose.yml`](../infra/docker-compose.yml) hosting PostgreSQL 17 (`postgres:17.4-alpine`) and Redis 7 (`redis:7.4.2-alpine`) with healthchecks, persistent named volumes, and host port bindings restricted to `127.0.0.1`.
- **Network Segmentation Intent**: Documented three isolated network zones: `core` (api, postgres, redis), `lab` (toy microservices), and `broker` (isolated execution service). The LLM worker and API will never join the `broker` network.

### Phase 0.3: Backend Toolchain & Health Probes
- **Package Management**: Single root [`pyproject.toml`](../pyproject.toml) managed via `uv`, with dependencies pinned to exact patch versions.
- **Settings & Config**: [`apps/api/settings.py`](../apps/api/settings.py) reading environment variables with strict validation, `SecretStr` for sensitive credentials, and fast failure on startup if required keys are missing.
- **Structured Redaction Logging**: [`apps/api/logging.py`](../apps/api/logging.py) configured with `structlog` and a custom filter that automatically redacts values for keys containing `key`, `secret`, `token`, and `password`.
- **Health Endpoints**:
  - `GET /healthz`: Immediate liveness check.
  - `GET /readyz`: Dependency connectivity probe verifying PostgreSQL and Redis, returning structured dependency statuses and responding with HTTP 503 if any dependency is degraded.

### Phase 0.4: Outcome Contract & Verification Spec
- **Contract Specification**: [`docs/outcome_contract.md`](outcome_contract.md) codifying what constitutes success, the verifiable signals required for outcome proof, stop-and-ask boundary conditions, a deterministic incident state machine, and explicit non-goals.
- **Architectural Decisions**: [`docs/DECISIONS.md`](DECISIONS.md) capturing rationale for package pinning, execution isolation, and SDK boundaries.

### Phase 0.5: Review UI Scaffold
- **Frontend Stack**: [`apps/web/`](../apps/web) scaffolded with React 19, Vite, TypeScript, ESLint, and Prettier.
- **Readiness Dashboard**: Live dashboard querying `GET /readyz` via typed API client [`client.ts`](../apps/web/src/api/client.ts).
- **Unit Testing**: Vitest and Testing Library verifying healthy and degraded UI rendering.

### Phase 0.7: Provider-Isolated Model Adapter
- **Protocol Interface**: [`agent/llm/interface.py`](../agent/llm/interface.py) defining `LLMClient` with `generate_structured` and `generate_with_tools`. Results carry parsed output, `TokenUsage`, `latency_ms`, `provider_request_id`, and typed `LLMError` taxonomies (`provider_error`, `timeout`, `schema_validation_failed`, `safety_blocked`).
- **Gemini Adapter**: [`agent/llm/gemini.py`](../agent/llm/gemini.py) wrapping official `google-genai` SDK with exponential backoff on transient errors (408, 429, 5xx), hard per-call timeout enforcement, and sensitive data prompt redaction.
- **Deterministic Replay Client**: [`agent/llm/fake.py`](../agent/llm/fake.py) for offline, deterministic testing and fixture replay without external API keys.
- **Token Budget Guard**: Cumulative `TokenBudgetGuard` raising typed `BudgetExceededError` when token thresholds are exceeded.

### Phase 0.8: Database Migrations, CI, & Reproducibility
- **Alembic Migrations**: [`alembic.ini`](../alembic.ini) and [`infra/alembic/`](../infra/alembic) wired to `DATABASE_URL` with an empty baseline migration (`0001_baseline`).
- **Lifecycle Makefile**: Updated `make up`, `make down`, and `make reset` targets.
- **CI Workflow**: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) validating backend lint/typecheck/test, web lint/test, socket script, and compose config.
- **Pre-commit Secret Scanning**: [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) enforcing Ruff formatting and Gitleaks secret detection before every commit.

---

## 3. Verification Suite & Exact Commands

### Comprehensive Offline Verification
```bash
# 1. Backend Linting
uv run ruff check .

# 2. Backend Strict Typechecking
uv run mypy apps agent tests

# 3. Full Backend Unit Tests (Postgres, Redis, LLM adapter, Alembic)
uv run pytest -v

# 4. Frontend Linting & Unit Tests
cd apps/web && npm run lint && npm test

# 5. Docker Socket Audit
bash scripts/check_no_docker_socket.sh

# 6. Docker Compose Config Validation
docker compose -f infra/docker-compose.yml config --quiet
```

### Live Smoke Testing (Optional)
```bash
export GEMINI_API_KEY="your-api-key"
export GEMINI_MODEL="gemini-2.0-flash"
uv run pytest -m live -v
```

---

## 4. Known Limits
1. **Lab Microservices Not Yet Seeded**: The toy services and fault injection harness belong to Feature 1; currently, only core persistence (PostgreSQL) and caching (Redis) are active.
2. **LangGraph Agent Execution**: The LangGraph state nodes and evidence-gathering read tools will be wired up in Feature 2 using the completed model adapter.
3. **Local Docker Environment**: OpsPilot is strictly scoped to local Docker Compose. Cloud providers, Kubernetes, and remote orchestration are explicit non-goals.

---

## 5. Architectural Decisions & Plan Deviations
1. **Google GenAI SDK Pinning (Decision 001)**: The latest `google-genai>=3.6.1` introduced a hard requirement for `pydantic>=2.12.5`, conflicting with the stable pinned `pydantic==2.10.6`. We resolved this by pinning `google-genai==2.8.0`, ensuring total compatibility without breaking dependency pins.
2. **WSL Node.js Environment (Decision 002)**: To prevent pull of over 600 bloated deb packages via distro apt in the WSL Linux environment, Node 22.14.0 LTS was installed cleanly via official standalone archive into `/usr/local`.
3. **Mypy Type Checking Optimization (Decision 003)**: Google GenAI's generated SDK contains over 30,000 lines of recursive type declarations. On DrvFS mounts, recursive traversal introduced multi-minute typechecking delays; we added `follow_imports = "skip"` for `google.*` in `pyproject.toml`, allowing strict typing of OpsPilot application code while dropping check times from 4.5 minutes to under 5 seconds.
4. **Isolated Alembic Unit Testing (Decision 004)**: To ensure `make test` runs 100% reliably in offline CI runners without requiring live Postgres socket bindings, [`tests/test_migrations.py`](../tests/test_migrations.py) tests clean database upgrades and downgrades programmatically with isolated test engines.
