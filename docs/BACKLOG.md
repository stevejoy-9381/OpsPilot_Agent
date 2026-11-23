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

## Agent & Review UI (Features 2-4 Planning)

1. **LangGraph Tool Budgeting Engine**:
   - Enforce hard limits on tool invocations (max Prometheus range queries, max trace lookups) per diagnosis step to prevent token explosion.
2. **Interactive Plan Diff Review**:
   - Visual side-by-side diffing of proposed service rollback or remediation actions inside the React UI before human cryptographic sign-off.
3. **Escalation Notification Webhooks**:
   - Webhook integration (Slack / PagerDuty / Email) when the agent identifies an unresolvable incident or halts due to safety boundary constraints.
