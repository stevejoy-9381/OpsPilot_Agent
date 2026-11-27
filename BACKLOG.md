# OpsPilot Backlog

This document tracks remaining post-Feature 9 implementation milestones.

- [ ] **Feature 10: Independent Outcome Verification (`evals/` & Verifier)**
  - Post-execution health probes (`GET /healthz`, `POST /ping`) and 30s synthetic load window ($E_{post} < 0.5\%$, $L_{post} \le 1.20 \times L_{baseline}$).
  - Persists `app.verification_results` and transitions `verifying -> completed` or `verifying -> escalated`.
- [ ] **Feature 11: Review UI (`apps/web`)**
  - Fact-driven Review UI consuming `GET /plans/{id}/review`, `POST /plans/{id}/decisions` (with `If-Match`, `Idempotency-Key`, CSRF), `GET /plans/{id}/decision`, and `GET /runs/{id}` execution audit trail.
  - Explicit `Untrusted AI Note` quarantine callout and live DB-anchored TTL countdown.
