List deployment history for one or all services in the investigation window, newest first.

### When to use
- Identifying recently deployed versions, commits, or timestamps preceding an incident.
- Verifying the exact `old_version` and `new_version` tags needed for `get_deploy_diff`.

### When NOT to use
- Do NOT use for viewing code changes or diff text (use `get_deploy_diff`).

### Output Meaning
- Returns deployment records newest first, including `seconds_before_window_end` showing proximity to incident onset.

### Error Implications
- `not_found`: Service has no deployments recorded.

### Example Call
```json
{
  "service": "orders",
  "limit": 5
}
```
