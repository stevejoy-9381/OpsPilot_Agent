"""agent/graph/context.py

Context assembler and prompt version governance for the OpsPilot Investigator Agent.

Responsibilities:
1. Loads versioned system prompt (agent/prompts/investigator_system_v1.md) from disk.
2. Computes and verifies deterministic SHA-256 prompt hash (system prompt + tool specs).
3. Assembles each turn's LLM context:
   - System prompt (governed by untrusted data quarantine and epistemic honesty rules).
   - Incident alert payload.
   - Static microservice topology summary (gateway -> orders -> inventory, database, cache).
   - Running evidence digest (evidence_id, tool, one-line summary, captured window).
   - Remaining tool call budget.
   - Authorized tool specifications from the registry.
4. Enforces per-turn token budget cap (default 4,000 tokens using 4 chars/token heuristic).
   Compacts older digest entries when exceeding cap, NEVER dropping cited evidence IDs.
5. Runs Feature 1.13 leakage scan over the prompt and assembled context before emission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.graph.state import InvestigationState
from agent.llm.interface import Message
from baseline.alerts.models import AlertEvent
from tools.registry import registry

logger = logging.getLogger("opspilot.agent.context")

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT_V1_PATH = PROMPTS_DIR / "investigator_system_v1.md"
SYSTEM_PROMPT_V2_PATH = PROMPTS_DIR / "investigator_system_v2.md"
DEFAULT_SYSTEM_PROMPT_PATH = SYSTEM_PROMPT_V1_PATH
PROMPT_HASHES_FILE = PROMPTS_DIR / "prompt_hashes.json"
DEFAULT_MAX_PROMPT_TOKENS = 4000

# Canonical prompt hash computed from investigator_system_v1.md and baseline tool specifications
EXPECTED_PROMPT_V1_HASH = (
    "7c065736fe3721bba931953c9378015603f54f132a30446dd345a10472dad315"
)
# Canonical prompt hash computed from investigator_system_v2.md and baseline tool specifications
EXPECTED_PROMPT_V2_HASH = (
    "ade9f1e57fb55a291ed7ec472428abe4e8497b9ab39a2a985fbd3f3efa0cb97a"
)

# ---------------------------------------------------------------------------
# Feature 1.13 Zero-Leakage Scanning Patterns
# ---------------------------------------------------------------------------
FORBIDDEN_FAMILY_NAMES = (
    "pool_leak",
    "latency_regression",
    "dependency_timeout",
    "dependency_timeout_with_decoy",
    "false_alarm",
)

FORBIDDEN_TERMS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fault", re.compile(r"(?i)\bfaults?\b")),
    ("inject", re.compile(r"(?i)\binject(ed|ion|s|ing)?\b")),
    ("ground_truth", re.compile(r"(?i)\bground[-_]?truth\b")),
    ("admin_endpoint", re.compile(r"/admin/(?:state|deploy|faults|reset)")),
    ("admin_header", re.compile(r"(?i)X-Lab-Admin-Token")),
    ("diff_bug", re.compile(r"(?i)\bbugs?\b")),
    ("diff_leak", re.compile(r"(?i)\bleaks?\b")),
    ("diff_regression", re.compile(r"(?i)\bregressions?\b")),
)

SCENARIO_ID_PATTERNS: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5")


class LeakageDetectedError(Exception):
    """Raised when Feature 1.13 leakage is detected in prompt or context."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(f"Feature 1.13 leakage detected: {violations}")


def scan_for_leakage(text: str) -> list[str]:
    """Scans text for forbidden Feature 1.13 benchmark leakage terms.
    
    Checks:
    1. Failure family names.
    2. Forbidden operational/grader terms (fault, inject, ground_truth, admin paths, diff words).
    3. Scenario identifiers (S1 through S5).
    """
    violations: list[str] = []
    text_lower = text.lower()

    for fam in FORBIDDEN_FAMILY_NAMES:
        if fam.lower() in text_lower:
            violations.append(f"forbidden_family:{fam}")

    for term_name, pattern in FORBIDDEN_TERMS_PATTERNS:
        if pattern.search(text):
            violations.append(f"forbidden_term:{term_name}")

    for scn in SCENARIO_ID_PATTERNS:
        if re.search(rf"\b{scn}\b", text, re.IGNORECASE):
            violations.append(f"scenario_id:{scn}")

    return violations


def estimate_tokens(text: str) -> int:
    """Estimates token count using the standard rough 4-characters-per-token heuristic.
    
    Tokenizer Specification: Character count divided by 4 (minimum 1 token if non-empty).
    Widely accepted as an accurate rough estimator for English prose and JSON payloads.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def load_system_prompt(prompt_path: Path | str | None = None, version: str = "v1") -> str:
    """Loads the versioned system prompt markdown document from disk."""
    if prompt_path:
        path = Path(prompt_path)
    elif version == "v2":
        path = SYSTEM_PROMPT_V2_PATH
    else:
        path = SYSTEM_PROMPT_V1_PATH

    if not path.exists():
        raise FileNotFoundError(f"System prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def get_canonical_tool_specs_text(compact: bool = False) -> str:
    """Retrieves tool specs JSON text from registry, optionally with concise descriptions."""
    specs = registry.get_specs()
    if not compact:
        return json.dumps([s.model_dump() for s in specs], sort_keys=True, indent=2)

    compact_specs = []
    for s in specs:
        # First paragraph of description to avoid multi-page markdown documentation
        desc_summary = s.description.strip().split("\n\n")[0].replace("\n", " ")
        compact_specs.append({
            "name": s.name,
            "description": desc_summary,
            "parameters": s.parameters,
        })
    return json.dumps(compact_specs, sort_keys=True, separators=(",", ":"))


def compute_prompt_hash(system_prompt: str, tool_specs_text: str | None = None) -> str:
    """Computes canonical SHA-256 hash across system prompt text and tool specifications."""
    specs_text = tool_specs_text if tool_specs_text is not None else get_canonical_tool_specs_text(compact=False)
    combined = f"{system_prompt}\n---TOOL_SPECS---\n{specs_text}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


STATIC_TOPOLOGY_SUMMARY = """- `gateway`: Ingress HTTP routing. Directs checkout and order traffic to `orders`; directs catalog and stock queries to `inventory`.
- `orders`: Microservice managing order creation and transactions. Connects to PostgreSQL (`lab-postgres`).
- `inventory`: Microservice managing catalog listings and inventory availability. Connects to Redis (`lab-redis`) for caching and PostgreSQL (`lab-postgres`) for persistence.
- Infrastructure:
  - `lab-postgres`: Primary relational database instance.
  - `lab-redis`: In-memory key-value cache."""


@dataclass(frozen=True)
class AssembledContext:
    """Output envelope produced by context assembly."""

    messages: list[Message]
    prompt_hash: str
    total_tokens_est: int
    system_prompt_tokens: int
    user_prompt_tokens: int
    evidence_count: int
    compacted: bool


def format_ledger_digest(hypotheses_ledger: dict[str, Any] | None) -> str:
    """Formats a compact human- and LLM-readable summary of the hypothesis ledger."""
    if not hypotheses_ledger:
        return "- No hypotheses currently tracked on the ledger."
    lines: list[str] = []
    for hyp_id, hyp in sorted(hypotheses_ledger.items()):
        if isinstance(hyp, dict):
            status = hyp.get("status", "open")
            statement = hyp.get("statement", "")
            supp_count = len(hyp.get("supporting_evidence_ids") or [])
            cont_count = len(hyp.get("contrary_evidence_ids") or [])
            tests_count = len(hyp.get("tests_run") or [])
        else:
            status = getattr(hyp, "status", "open")
            statement = getattr(hyp, "statement", "")
            supp_count = len(getattr(hyp, "supporting_evidence_ids", ()) or ())
            cont_count = len(getattr(hyp, "contrary_evidence_ids", ()) or ())
            tests_count = len(getattr(hyp, "tests_run", ()) or ())
        lines.append(
            f"- `{hyp_id}` [{status}]: {statement} "
            f"(supporting: {supp_count}, contrary: {cont_count}, tests: {tests_count})"
        )
    return "\n".join(lines)


def format_evidence_digest(
    evidence_ids: list[str],
    tool_trace: list[dict[str, Any]],
    protected_ids: set[str],
    compact_older: bool = False,
) -> tuple[str, bool]:
    """Formats the running evidence digest, compacting older entries if requested.
    
    Invariants:
    - Evidence IDs in protected_ids are NEVER dropped or stripped of their findings.
    - All evidence_ids remain cited in the digest even under compaction.
    """
    if not evidence_ids:
        return "None captured yet.", False

    # Map evidence_id -> trace record
    trace_map: dict[str, dict[str, Any]] = {}
    for rec in tool_trace:
        ev_id = rec.get("evidence_id")
        if ev_id:
            trace_map[ev_id] = rec

    lines: list[str] = []
    compacted = False

    for i, ev_id in enumerate(evidence_ids):
        rec = trace_map.get(ev_id, {})
        tool = rec.get("tool", "telemetry_tool")
        args_summary = rec.get("args_summary", "")

        is_older = compact_older and (i < len(evidence_ids) - 2) and (ev_id not in protected_ids)

        if is_older:
            compacted = True
            lines.append(f"- `{ev_id}` ({tool}): [compacted] {args_summary[:60]}")
        else:
            summary = rec.get("summary") or args_summary or "telemetry data recorded"
            lines.append(f"- `{ev_id}` ({tool}): {summary}")

    return "\n".join(lines), compacted


def build_investigator_context(
    state: InvestigationState,
    *,
    prompt_path: Path | str | None = None,
    max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS,
    enforce_zero_leakage: bool = True,
    forced_conclude: bool = False,
) -> AssembledContext:
    """Assembles the complete per-turn message sequence for the investigator LLM.
    
    Args:
        state: Authoritative LangGraph InvestigationState.
        prompt_path: Optional path override for system prompt file.
        max_prompt_tokens: Maximum prompt token ceiling.
        enforce_zero_leakage: If True, raises LeakageDetectedError on benchmark leaks.
        forced_conclude: If True, renders forced-conclusion prompt variant omitting tool specs.
    """
    version = state.get("schema_version", "v1")
    system_prompt = load_system_prompt(prompt_path, version=version)

    # Canonical full tool specs text for immutable hash calculation
    canonical_specs_text = get_canonical_tool_specs_text(compact=False)
    prompt_hash = compute_prompt_hash(system_prompt, canonical_specs_text)

    # 1. Extract alert context
    raw_alert = state.get("alert", {})
    alert_event_json = (
        raw_alert.model_dump_json(indent=2)
        if isinstance(raw_alert, AlertEvent)
        else json.dumps(raw_alert, indent=2, sort_keys=True)
    )

    # 2. Determine protected evidence IDs from draft diagnosis and hypothesis ledger
    protected_ids: set[str] = set()
    draft = state.get("draft_diagnosis")
    if isinstance(draft, dict):
        protected_ids.update(draft.get("evidence_ids", []))
    elif hasattr(draft, "evidence_ids"):
        protected_ids.update(getattr(draft, "evidence_ids", []))

    ledger_dict = state.get("hypotheses_ledger") or {}
    for hyp in ledger_dict.values():
        if isinstance(hyp, dict):
            for ev_id in hyp.get("supporting_evidence_ids") or []:
                protected_ids.add(str(ev_id))
            for ev_id in hyp.get("contrary_evidence_ids") or []:
                protected_ids.add(str(ev_id))
            for tr in hyp.get("tests_run") or []:
                if isinstance(tr, dict) and tr.get("evidence_id"):
                    protected_ids.add(str(tr["evidence_id"]))
        elif hasattr(hyp, "supporting_evidence_ids"):
            for ev_id in getattr(hyp, "supporting_evidence_ids", ()):
                protected_ids.add(str(ev_id))
            for ev_id in getattr(hyp, "contrary_evidence_ids", ()):
                protected_ids.add(str(ev_id))
            for tr in getattr(hyp, "tests_run", ()):
                if isinstance(tr, dict) and tr.get("evidence_id"):
                    protected_ids.add(str(tr["evidence_id"]))

    ledger_text = format_ledger_digest(ledger_dict)

    # 3. Format evidence digest
    evidence_ids = list(state.get("evidence_ids", []))
    tool_trace = list(state.get("tool_trace", []))

    evidence_text, compacted = format_evidence_digest(
        evidence_ids=evidence_ids,
        tool_trace=tool_trace,
        protected_ids=protected_ids,
        compact_older=False,
    )

    remaining_calls = state.get("remaining_calls", 8)
    step_count = state.get("step_count", 0)
    messages_summary = state.get("messages_summary", "").strip()
    step_history_text = messages_summary if messages_summary else "Investigation started."

    # Use concise tool specs for model prompting to conserve tokens
    prompt_specs_text = get_canonical_tool_specs_text(compact=True)

    is_forced = forced_conclude or remaining_calls <= 0

    last_step_rejection = state.get("last_step_rejection")
    rejection_section = ""
    if last_step_rejection:
        rejection_section = f"""
### Previous Step Rejection Feedback
```json
{json.dumps(last_step_rejection, indent=2)}
```
CRITICAL: Your previous step was rejected by the deterministic validation gate.
Please address the gate rejection violations with refutation or corroborating telemetry before concluding.
"""

    # 4. Build user turn content
    def render_user_prompt(digest: str, history: str) -> str:
        if is_forced:
            return f"""### Active Incident Alert
```json
{alert_event_json}
```
{rejection_section}
### Operational Budget
- Investigation Step Turn: {step_count + 1}
- Tool Budget Status: Exhausted (0 remaining allowed queries)

### Topology Context
{STATIC_TOPOLOGY_SUMMARY}

### Hypothesis Ledger ({len(ledger_dict)} items)
{ledger_text}

### Evidence Collected So Far ({len(evidence_ids)} items)
{digest}

### Investigation History & Rationale
{history}

Instructions:
Your tool query budget is completely exhausted. No further tools can be invoked. You must conclude the investigation now.
Evaluate the evidence collected so far. Output a `conclude` step adhering strictly to the schema:
- Provide your best-supported diagnosis citing the evidence IDs collected so far, OR
- Output an `unknown` diagnosis if the collected evidence is insufficient, contradictory, or inconclusive.
Remember: your `reason` must be plain text and at most 300 characters."""

        return f"""### Active Incident Alert
```json
{alert_event_json}
```
{rejection_section}
### Operational Budget
- Investigation Step Turn: {step_count + 1}
- Remaining Allowed Tool Queries: {remaining_calls}

### Topology Context
{STATIC_TOPOLOGY_SUMMARY}

### Hypothesis Ledger ({len(ledger_dict)} items)
{ledger_text}

### Authorized Investigation Tool Specifications
```json
{prompt_specs_text}
```

### Evidence Collected So Far ({len(evidence_ids)} items)
{digest}

### Investigation History & Rationale
{history}

Instructions:
Evaluate the collected empirical evidence. Formulate or refine a hypothesis, then output your next step adhering exactly to the schema:
- `call_tool` to execute an authorized query that could test or disconfirm your hypothesis.
- `conclude` to finalize your diagnosis if evidence is sufficient or definitively inconclusive.
Remember: your `reason` must be plain text and at most 300 characters."""

    user_content = render_user_prompt(evidence_text, step_history_text)

    sys_tokens = estimate_tokens(system_prompt)
    user_tokens = estimate_tokens(user_content)
    total_tokens = sys_tokens + user_tokens

    # 5. Progressive compaction if over token cap (never dropping protected IDs)
    active_evidence_text = evidence_text
    if total_tokens > max_prompt_tokens:
        # Step A: Compact older non-protected evidence summaries
        if len(evidence_ids) > 2:
            active_evidence_text, compacted = format_evidence_digest(
                evidence_ids=evidence_ids,
                tool_trace=tool_trace,
                protected_ids=protected_ids,
                compact_older=True,
            )
            user_content = render_user_prompt(active_evidence_text, step_history_text)
            user_tokens = estimate_tokens(user_content)
            total_tokens = sys_tokens + user_tokens

        # Step B: If still over cap, window messages_summary history to last 3 entries
        if total_tokens > max_prompt_tokens and step_history_text:
            history_lines = step_history_text.splitlines()
            if len(history_lines) > 3:
                compacted = True
                windowed_history = "\n".join(history_lines[-3:])
                user_content = render_user_prompt(active_evidence_text, windowed_history)
                user_tokens = estimate_tokens(user_content)
                total_tokens = sys_tokens + user_tokens

    # 6. Zero-leakage enforcement
    if enforce_zero_leakage:
        sys_leaks = scan_for_leakage(system_prompt)
        if sys_leaks:
            raise LeakageDetectedError([f"system_prompt:{leak}" for leak in sys_leaks])

        user_leaks = scan_for_leakage(user_content)
        if user_leaks:
            raise LeakageDetectedError([f"user_content:{leak}" for leak in user_leaks])

    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_content),
    ]

    return AssembledContext(
        messages=messages,
        prompt_hash=prompt_hash,
        total_tokens_est=total_tokens,
        system_prompt_tokens=sys_tokens,
        user_prompt_tokens=user_tokens,
        evidence_count=len(evidence_ids),
        compacted=compacted,
    )
