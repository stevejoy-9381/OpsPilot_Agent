# OpsPilot Backlog

This document tracks remaining post-Feature 8 implementation milestones.

- [ ] **Feature 9: Isolated Execution Broker (`broker/`)**
  - Reads `action_executions(status='queued')` and `action_plans(status='approved')`.
  - Repeats pre-flight target-version verification via `BrokerTargetCheckPort`.
  - Executes scoped `/admin/actions/*` mutations with `action_key` idempotency and `uncertain` reconciliation.
- [ ] **Feature 10: Independent Outcome Verification (`evals/` & Verifier)**
  - Post-execution health probes (`GET /healthz`, `POST /ping`) and 30s synthetic load window ($E_{post} < 0.5\%$, $L_{post} \le 1.20 \times L_{baseline}$).
  - Persists `app.verification_results` and transitions `verifying -> completed` or `verifying -> escalated`.
- [ ] **Feature 11: Review UI (`apps/web`)**
  - Fact-driven Review UI consuming `GET /plans/{id}/review`, `POST /plans/{id}/decisions` (with `If-Match`, `Idempotency-Key`, CSRF), and `GET /plans/{id}/decision`.
  - Explicit `Untrusted AI Note` quarantine callout and live DB-anchored TTL countdown.
