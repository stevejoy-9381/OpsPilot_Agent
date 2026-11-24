Query numeric time-series telemetry for a specific microservice.

### When to use
- Measuring error rates, request throughput, or latencies (p50/p95/p99) over time.
- Verifying database connection pool utilization or wait times.
- Checking external dependency call latencies or failure ratios.

### When NOT to use
- Do NOT use for finding specific log messages, exceptions, or stack traces (use `search_logs`).
- Do NOT use for inspecting individual request execution spans (use `get_trace`).

### Output Meaning & Units
- Returns downsampled time series points (`t`, `v`), statistical summary (`min`, `max`, `mean`, `first`, `last`, `trend`), and baseline comparison over preceding window.
- Units: `requests_per_second` (request_rate), `ratio` (error_rate), `seconds` (latency/wait), `count` (db_pool_in_use).

### Error Implications
- `not_found`: Service is not emitting this metric or service name is incorrect.
- `stale_data`: Telemetry pipeline is lagging or service stopped emitting samples.

### Example Call
```json
{
  "service": "orders",
  "metric": "db_pool_in_use",
  "start": "2026-03-30T10:00:00Z",
  "end": "2026-03-30T10:15:00Z"
}
```
