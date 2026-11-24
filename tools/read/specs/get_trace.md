Retrieve full distributed span tree by trace ID, or search candidate trace IDs.

### When to use
- Investigating distributed request bottlenecks, slow database queries, or downstream RPC timeouts.
- In candidate mode (`service` provided), finding the slowest or errored trace IDs to inspect next.

### When NOT to use
- Do NOT use when both `trace_id` and `service` are passed (they are mutually exclusive).
- Do NOT use for aggregate metric overviews (use `get_metrics`).

### Output Meaning
- Full trace mode: Returns hierarchical spans capped at 40 spans (collapsing repeated sibling spans), critical path, slowest span, and per-dependency duration breakdown.
- Candidate mode: Returns top matching trace IDs with one-line summaries.

### Error Implications
- `not_found`: Trace ID does not exist or expired from storage.
- `out_of_window`: Trace started outside the authorized investigation window.

### Example Call
```json
{
  "trace_id": "tr_s2_nplus1"
}
```
