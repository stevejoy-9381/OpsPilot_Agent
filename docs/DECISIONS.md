# Architecture Decision Records (ADRs)

This document records the architectural and design decisions for OpsPilot.

---

## ADR-001: LangGraph with PostgreSQL Checkpointing for Agent State

### Context
OpsPilot requires a multi-step investigation workflow where the LLM worker cycles between reading telemetry, formulating hypotheses, checking stop conditions, and generating a recovery plan. This stateful workflow must support:
- Deterministic interruptions (pausing for human approval).
- Resumption after approval or re-investigation triggers.
- Immutable auditing of thought steps, evidence pointers, and tool calls.
- Crash resilience without losing in-flight investigation context.

### Decision
We use **LangGraph** backed by **`langgraph-checkpoint-postgres`** (`PostgresSaver`) for workflow orchestration. All state transitions, node inputs, and intermediate evidence artifacts are persisted directly to the `core` PostgreSQL database.

### Consequences
- **Positive**: Workflows can safely halt at `needs_review` and resume across distinct processes or server restarts.
- **Positive**: Complete auditability of every reasoning and tool-call step in PostgreSQL.
- **Negative**: Adds a direct dependency on PostgreSQL schema migrations and table maintenance for checkpoints.
- **Mitigation**: Schema migrations are managed deterministically via Alembic.

---

## ADR-002: Separate Isolated Broker Service for Lab Control

### Context
OpsPilot recovers toy lab services by executing configuration rollbacks, container restarts, or parameter changes. Allowing the LLM worker or the public-facing API to access the Docker daemon socket or container management credentials creates severe prompt injection risks: a compromised model could escape its scope, kill host processes, or perform unapproved actions.

### Decision
We isolate all container manipulation capabilities into a separate **`broker`** service:
- The Docker socket (`/var/run/docker.sock`) is mounted **only** into the `broker` container.
- The `broker` runs on an isolated internal network (`broker`) and is never reachable by the LLM worker.
- The API writes an approved plan record with an exact cryptographic hash (`plan_hash`) to the database.
- The broker reads the approval record, validates that the hash matches, checks the expiration TTL, acquires an idempotency lock, executes the plan, and reports the result.

### Consequences
- **Positive**: Complete security boundary. An exploited or prompt-injected LLM worker has zero network or socket path to touch lab containers.
- **Positive**: Execution is strictly deterministic and auditable.
- **Negative**: Requires inter-service coordination between API and Broker via database or queue.
- **Mitigation**: State and idempotency tokens are tracked in PostgreSQL with row-level locks.

---

## ADR-003: Gemini LLM Isolated Behind Internal Model Adapter

### Context
The agent requires an LLM to interpret unstructured logs, correlate metrics, and generate recovery proposals. Directly scattering provider SDK calls (such as `google-genai` or `langchain-google-genai`) across tool nodes and graph code couples the application to provider-specific message schemas, token counters, and API idiosyncrasies.

### Decision
All LLM interactions are isolated behind a single model adapter interface in **`agent/llm/`**:
- The agent graph interacts solely with internal Pydantic message contracts and tool definitions.
- The adapter translates internal contracts into Gemini API format, enforces request timeouts, handles structured tool calling, and standardizes error responses.
- The Gemini API key and model identifier are injected strictly via settings (`GEMINI_API_KEY`, `GEMINI_MODEL`) and masked from logs.

### Consequences
- **Positive**: Decouples graph logic from provider changes and deprecations.
- **Positive**: Enables deterministic mocking and offline fixture playback during CI/CD evaluation without real API keys.
- **Negative**: Requires maintaining an internal translation layer.
- **Mitigation**: Internal adapter interface is kept narrow and typed with Pydantic v2 schemas.

---

## ADR-004: React + Vite + TypeScript for Review UI

### Context
A core rule of OpsPilot is that an authenticated human must review evidence and approve recovery plans before any action is executed. Operators require a fast, clean interface to view side-by-side evidence, inspect exact action diffs, and sign off on plan hashes.

### Decision
We use a **React + Vite (TypeScript)** single-page application scaffolded in `apps/web`:
- High-performance developer feedback with Vite.
- Static typing shared with API client contracts.
- Isolated from API logic via an explicit typed API client (`apps/web/src/api/client.ts`).
- CORS configured on the FastAPI server strictly allowing `http://localhost:5173` with no wildcards.

### Consequences
- **Positive**: Fast local iteration, modern component architecture, and lightweight production bundle.
- **Positive**: Comprehensive UI testing with Vitest and React Testing Library without running a full browser.
- **Negative**: Maintains a separate Node/TypeScript toolchain alongside Python.
- **Mitigation**: Toolchain commands are wrapped in root Makefile targets (`web-install`, `web-dev`, `web-test`, `web-lint`).

---

## ADR-005: Strict Docker Compose Local Lab Scope

### Context
Testing automated incident investigation on production infrastructure, cloud providers, or Kubernetes clusters is hazardous, expensive, and difficult to reproduce reliably.

### Decision
OpsPilot operates strictly against a local **Docker Compose** lab of synthetic toy services:
- All services, databases, and message brokers run as local Compose services bound to `127.0.0.1`.
- Incidents are synthetically injected (e.g., config corruption, simulated load spikes, synthetic database locks).
- The repository enforces that no external cloud credentials, Kubernetes configurations, or production endpoints are used.

### Consequences
- **Positive**: Deterministic, 100% reproducible incident scenarios and evaluations.
- **Positive**: Zero cloud costs, zero accidental disruption to real workloads.
- **Positive**: Can run completely air-gapped on developer workstations.
- **Negative**: Does not test multi-cloud or distributed edge topologies (acknowledged non-goal).

---

## ADR-006: LangGraph Checkpointer Versions & Interrupt API Behavior

### Context
Feature 8 introduces durable human-in-the-loop pause/resume (`await_review` using `langgraph.types.interrupt` and `Command(resume=...)`) backed by `PostgresSaver` on the dedicated `checkpoints` schema (`opspilot_checkpointer` role).

### Pinned Versions
- `langgraph==0.3.5`
- `langgraph-checkpoint-postgres==2.0.16`
- `langgraph-checkpoint==2.1.2`

### Observed API Quirks & Mitigations
1. **`graph.invoke()` Interrupt Return Shape**: In `langgraph==0.3.5`, `graph.invoke(input, config)` returns an `AddableValuesDict` **without** an `'__interrupt__'` key when a node calls `interrupt()`.
2. **`StateSnapshot` Interrupt Inspection**: `graph.get_state(config)` returns a `StateSnapshot` where pending interrupts are stored on `snap.tasks[0].interrupts` (not `snap.interrupts`), and `snap.next` contains `('await_review',)`.
3. **`PostgresSaver` Connection Settings**: `PostgresSaver(conn)` requires `autocommit=True`, `prepare_threshold=0`, `row_factory=dict_row`, and `SET search_path = checkpoints, public;` so checkpointer tables reside strictly in `checkpoints.*`.
4. **Mitigation**: Both `WorkerService.process_run()` and `WorkerService.resume_interrupted_run()` inspect `graph.get_state(config).next` (and fallback `'__interrupt__'` check) rather than relying solely on `'__interrupt__'` in the `invoke()` return dict.
