# OpsPilot Project Backlog

Deferred ideas, future enhancements, and follow-ups identified during architecture and implementation.

---

## Lab & Evaluation Substrate (Feature 1 Follow-ups)

1. **Kernel-Level Chaos / Network Shims (ToxiProxy)**:
   - *Context*: Feature 1 implements in-process latency and error rate injection at the FastAPI middleware layer.
   - *Idea*: Introduce an optional ToxiProxy sidecar on `lab-admin` if packet-level drops, TCP resets, or asymmetric routing degradations are needed for deeper network failure simulations.
   - *Status*: Deferred to post-MVP.

2. **Multi-Region & Replication Topology**:
   - *Context*: The current lab runs on a single host with co-located microservices.
   - *Idea*: Add cross-region replication lag simulation between primary and replica PostgreSQL databases, simulating split-brain or stale read anomalies.
   - *Status*: Deferred.

3. **OTel GRPC Streaming Sink**:
   - *Context*: Feature 1 exports traces as JSONL lines to a shared volume (`/telemetry/traces/spans.jsonl`).
   - *Idea*: Add support for direct OTLP gRPC streaming to an external collector or Jaeger instance when visual trace timeline inspection is desired by human evaluators.
   - *Status*: Deferred.

4. **Dynamic Unscheduled Faults (Chaos Monkey)**:
   - *Context*: Scenarios in Feature 1 follow deterministic timelines.
   - *Idea*: Build an unscripted chaos mode where secondary independent faults fire randomly during an incident run to test the agent's resilience against noisy or compounding failures.
   - *Status*: Deferred to Feature 4 verification evaluations.

5. **Extended Load Profiles**:
   - *Context*: `lab/loadgen` provides steady, burst, and ramp profiles with Zipf distribution.
   - *Idea*: Add sinusoidal diurnal cycle patterns and stepped degradation profiles for multi-hour soak tests.
   - *Status*: Deferred.

---

## Read Tools & Evidence (Feature 2 Follow-ups)

1. **Persistent PostgreSQL `observations` Storage**:
   - *Context*: Feature 2 implements `InMemoryEvidenceStore` and `JsonlEvidenceStore`.
   - *Idea*: Migrate evidence audit records into permanent PostgreSQL `observations` table during Feature 4 persistence milestone.
   - *Status*: Completed in Feature 4 (`PostgresEvidenceStore` with 64 KB inline cap, `payload_ref`, content hash verification, and RLS).

2. **Distributed Trace Visualizer in Review UI**:
   - *Context*: `get_trace` provides precomputed critical paths and collapsed span trees via CLI/API.
   - *Idea*: Add a visual waterfall chart component in React UI using the `SpanNode` schema.
   - *Status*: Slated for Feature 8 UI enhancements.

---

## PostgreSQL Data Model & Invariants (Feature 4 Follow-ups)

1. **Physical Broker Role Grant Finalization**:
   - *Context*: Role `broker` is provisioned with read grants on `app.action_plans` and write grants on `app.action_executions`.
   - *Idea*: Finalize broker daemon credentials, mutual TLS / service token exchange, and execution feedback loop during Feature 9 (Execution Broker).
   - *Status*: Slated for Feature 9.

2. **PostgreSQL Checkpointer Integration with Agent Graph**:
   - *Context*: Schema `checkpoints` and role `opspilot_checkpointer` are fully provisioned and verified with LangGraph `setup()`.
   - *Idea*: Wire LangGraph `PostgresSaver` directly into `agent/graph/` during Feature 6 agent construction.
   - *Status*: Slated for Feature 6.

3. **Partitioning on `app.run_events` and `app.observations`**:
   - *Context*: Large-scale deployments will generate millions of telemetry events and observation records.
   - *Idea*: Introduce declarative range partitioning on `created_at` or list partitioning on `tenant_id` for automated retention management and cold-storage tiering.
   - *Status*: Deferred to post-MVP scale hardening.

---

## Baseline Evaluation Ladder (Feature 3 Follow-ups)

1. **Apply-and-Verify Mode for Baselines**:
   - *Context*: Baselines B0, B1, and B2 are currently evaluated in dry-run mode against canonical ground truth.
   - *Idea*: Connect the baseline ladder to the real execution broker and Oracle recovery verifier (requires Feature 10) to physically execute baseline recommendations in live container environments and measure actual MTTR and service recovery rate.
   - *Status*: Slated for Feature 10.

2. **Three-Trial Multi-Sampling for Live B2 (Gemini)**:
   - *Context*: Initial B2 live baseline benchmark was specified with `trials=1`.
   - *Idea*: Run 3 trials per scenario (`--trials 3`) with temperature sampling variations or confidence intervals once live Gemini API key quotas are provisioned in the evaluation environment.
   - *Status*: Slated for Feature 13 / pre-release benchmarking.

3. **Final Comparison on 60-Case Benchmark Set (Feature 13)**:
   - *Context*: Development benchmarking was performed against the initial 5 scenarios (S1–S5).
   - *Idea*: Benchmark B0, B1, B2, and the full OpsPilot agent across the complete 60-incident sealed evaluation suite under Feature 13 to prove the agent value gate with statistical significance.
   - *Status*: Slated for Feature 13.

---

## Roadmap Alignment & Upcoming Milestones

### Feature 6: Hypothesis Tracking & Contrary Evidence Formulation
- **Explicit Dual-Hypothesis Tracking**: Model formulates `primary_hypothesis` and competing alternative on every turn.
- **Contrary Evidence Search**: Require at least one query intended to refute or disconfirm the leading hypothesis before attributing causality (addressing decoy deploy scenarios like S4).
- **Candidate v2 Prompt Transition**: Formal migration from `investigator_system_v1.md` to `investigator_system_v2.md` following version governance and cryptographic hash verification.
- **Quota-Aware Query Planning**: Heuristic and prompt guidance to batch high-information queries (`list_deploys` + `search_logs`) in initial turns to preserve API quota.

### Feature 7: Action Proposals, Remediation Planning & Approval Gates
- **Remediation Plan Synthesis**: Translate confirmed diagnosis into structured action plans using the closed vocabulary in `shared/actions.py` (`rollback_service`, `restart_service`, `no_action`, `escalate`).
- **Cryptographic Plan Hashing**: Enforce SHA-256 plan hash computation matching `app.action_plans.plan_hash` database invariant triggers.
- **Human-in-the-Loop Approval Gate**: Status transition `needs_review -> awaiting_approval` preventing execution until explicit cryptographic human sign-off.
- **Review UI Action Timeline**: Interactive plan diffing, risk assessment badges, and approval/rejection action bar.

### Feature 8: Durable Resume, Checkpointing & Recovery Engine
- **PostgreSQL Checkpointer Integration**: Wire LangGraph `PostgresSaver` into the `checkpoints` schema with connection pooling and RLS.
- **Durable Pause-and-Resume**: Seamlessly freeze state during human approval or provider backoff and resume execution on worker restart without re-running earlier steps.
- **Distributed Trace Visualizer**: Add waterfall timeline charts in React UI for visual root cause validation.
- **Escalation Notification Webhooks**: Dispatch Slack/PagerDuty webhooks upon `provider_error_exhausted`, `budget_exhausted`, or manual human escalation.
