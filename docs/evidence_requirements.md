# Evidence Requirements Specification

This document defines the minimum observational signals required by an investigator
(human or autonomous agent) to diagnose each failure family unambiguously.
These requirements establish the contract for the Feature 2 Investigation Tooling.

---

## 1. Summary of Required Signals by Failure Family

### `pool_leak`
- **Metric Signals**: `db_pool_in_use == db_pool_size`, `http_requests_total{status='503'}` spike.
- **Log Events**: `db_pool_timeout`, `order_conflict` (409).
- **Trace Indicators**: Elevated `gateway.checkout` duration ending in 503.
- **Deploy Audit Signals**: Recent `orders` deploy modifying database pool handling.

### `latency_regression`
- **Metric Signals**: Elevated `http_request_duration_seconds{le='0.5'}` p95, zero 5xx.
- **Log Events**: Absence of Redis cache hit logs, elevated database query log frequency.
- **Trace Indicators**: `inventory` span breakdown dominated by downstream DB queries.
- **Deploy Audit Signals**: Recent `inventory` deploy altering caching strategy.

### `dependency_timeout`
- **Metric Signals**: `dependency_call_duration_seconds{dependency='redis'}` > 150ms.
- **Log Events**: `dependency_timeout` on Redis, datastore fallback log.
- **Trace Indicators**: Parent span delay attributed entirely to child dependency span.
- **Deploy Audit Signals**: Zero deployments in incident window.

### `dependency_timeout_with_decoy`
- **Metric Signals**: Redis call duration elevated beyond timeout threshold.
- **Log Events**: Redis timeout and fallback logs.
- **Trace Indicators**: Child span latency isolating problem to Redis.
- **Deploy Audit Signals**: Benign deploy on `orders` (decoy) with cosmetic diff.

### `false_alarm`
- **Metric Signals**: Transient surge in `http_requests_total` rate with 100% 2xx status.
- **Log Events**: Transient traffic increase, zero error or timeout events.
- **Trace Indicators**: Latencies within bounded capacity SLA limits.
- **Deploy Audit Signals**: Zero deployments or configuration changes.

---

## 2. Tool Design Implications for Feature 2 Agent Tooling

1. **Prometheus Range Tool (`query_range`)**:
   - Instant rate/p95 calculations across customizable windows (`5m`, `15m`).
   - Expose connection pool utilization metrics (`db_pool_in_use` vs `db_pool_size`).
2. **Log Search Tool (`search_logs`)**:
   - Must support filtering by service and minimum log level (`WARN`, `ERROR`).
   - Must correlate structured event names (`db_pool_timeout`, `dependency_timeout`).
3. **Trace Inspection Tool (`get_trace`)**:
   - Must return parent-child span hierarchy with per-span duration attribution.
4. **Deploy History Tool (`get_deploy_history`)**:
   - Must present chronological diffs and changed files leading up to the window.
   - Must enable comparing metrics before and after deployment timestamps.
