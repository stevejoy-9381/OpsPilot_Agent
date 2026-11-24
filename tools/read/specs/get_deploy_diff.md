Retrieve git diff and configuration changes between two deployment versions of an authorized service.

### When to use
- Inspecting code or configuration changes introduced by a suspect deployment.
- Confirming root cause mechanisms such as pool config edits, query loops, or timeout changes.

### When NOT to use
- Do NOT use with version tags that were not deployed before `window_end` (they are invisible).
- Do NOT use across different services.

### Output Meaning
- Returns changed file statistics (lines added/deleted), detected config modifications, and redacted/quarantined diff text.

### Error Implications
- `not_found`: One or both versions do not exist in the deploy catalog prior to `window_end`.
- `access_denied`: Service is not in authorized investigation services.

### Example Call
```json
{
  "service": "orders",
  "old_version": "v1.1.0",
  "new_version": "v1.2.0"
}
```
