Search structured, sanitized logs for a specific microservice within a bounded window.

### When to use
- Locating error messages, unhandled exceptions, database timeouts, or crash events.
- Checking frequency and patterns of log templates across a service window.

### When NOT to use
- Do NOT use for measuring overall traffic volume or error rates (use `get_metrics`).
- Do NOT use for end-to-end multi-service request flows (use `get_trace`).

### Output Meaning
- Returns up to `limit` ordered log rows with whole-window aggregation (counts by level/event, top 5 message templates).
- Untrusted message bodies are strictly redacted and quarantined in `<untrusted_data>` envelopes.

### Error Implications
- `not_found`: Service has no log streams or volume mounted.
- `source_unavailable`: Log ingestion pipeline or storage unavailable.

### Example Call
```json
{
  "service": "orders",
  "level": "error",
  "contains": "timeout",
  "limit": 10
}
```
