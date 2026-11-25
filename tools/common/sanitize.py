"""Deterministic sanitization and untrusted data quarantine for tool outputs.

Provides:
- redact: Mask secrets, bearer tokens, API keys, passwords, JWTs, connection credentials,
  long hex/base64 secrets, emails, and environment tokens (LAB_ADMIN_TOKEN, HARNESS_TOKEN).
- quarantine: Wrap untrusted text in a safe XML-style envelope, escape closing tags,
  strip control/zero-width characters, truncate with marker.
- detect_instruction_like_text: Flag prompt injection / instruction-like phrases for notes.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

# ---------------------------------------------------------------------------
# Instruction Detection Patterns
# ---------------------------------------------------------------------------
INSTRUCTION_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|prompts?|commands?)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byou\s+must\s+(?:now\s+)?(?:run|execute|approve|ignore|override)\b", re.IGNORECASE),
    re.compile(r"\brun\s+(?:command|shell|script|terminal)\b", re.IGNORECASE),
    re.compile(r"\b(?:approve|rollback)\s+now\b", re.IGNORECASE),
    re.compile(r"\badmin\s+override\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+(?:report|log|alert|disclose)\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:the\s+)?above\b", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# Secret Redaction Patterns
# ---------------------------------------------------------------------------
# Bearer tokens
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)

# Standard JWT format: three base64url segments separated by dots
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\b"
)

# Connection strings with credentials: scheme://user:pass@host:port/db
_CONN_STRING_PATTERN = re.compile(
    r"(?i)\b([a-zA-Z][a-zA-Z0-9+\-.]*://[^:\s/@]+):([^@\s/]+)@",
)

# Common API Key / Secret assignments: key=..., token=..., secret=..., password=...
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret|"
    r"private[_-]?key|passwd|password)\s*[:=]\s*['\"]?([A-Za-z0-9\-._~+/=]{8,})['\"]?"
)

# Common API Key prefixes: sk_live_..., ghp_..., glpat-..., xoxb-..., etc.
_PREFIXED_KEY_PATTERN = re.compile(
    r"\b(?:sk_live_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9\-]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"
)

# Generic long hex/base64 strings (32+ hex chars or 40+ base64 chars that look like raw hashes/keys)
_LONG_HEX_PATTERN = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32,64}(?![a-fA-F0-9])")
_LONG_BASE64_PATTERN = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")

# Email addresses
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Control characters (excluding \n, \r, \t) and zero-width/bidi formatting characters
_CONTROL_AND_ZERO_WIDTH_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)


def detect_instruction_like_text(text: str) -> list[str]:
    """Detect if text contains prompt-injection or instruction-like phrases."""
    notes: list[str] = []
    for pattern in INSTRUCTION_PATTERNS:
        if pattern.search(text):
            notes.append("possible_instruction_like_text")
            break
    return notes


def redact(text: str) -> str:
    """Mask secrets, credentials, tokens, and emails with deterministic markers."""
    if not text:
        return text

    # Redact dynamic environment tokens if present
    for env_var in ("LAB_ADMIN_TOKEN", "HARNESS_TOKEN"):
        env_val = os.getenv(env_var)
        if env_val and len(env_val) >= 4:
            text = text.replace(env_val, f"[REDACTED:{env_var}]")

    # Redact Bearer tokens
    text = _BEARER_PATTERN.sub("Bearer [REDACTED:BEARER_TOKEN]", text)

    # Redact JWTs
    text = _JWT_PATTERN.sub("[REDACTED:JWT]", text)

    # Redact connection-string passwords
    text = _CONN_STRING_PATTERN.sub(r"\1:[REDACTED:PASSWORD]@", text)

    # Redact known API key prefix formats
    text = _PREFIXED_KEY_PATTERN.sub("[REDACTED:API_KEY]", text)

    # Redact key=value style passwords and tokens
    def _replace_kv(m: re.Match[str]) -> str:
        key_name = m.group(1)
        return f"{key_name}=[REDACTED:SECRET]"

    text = _KEY_VALUE_SECRET_PATTERN.sub(_replace_kv, text)

    # Redact emails
    text = _EMAIL_PATTERN.sub("[REDACTED:EMAIL]", text)

    # Redact long hex secrets (skip if already marked [REDACTED:...)
    def _replace_hex(_m: re.Match[str]) -> str:
        # Avoid replacing inside existing replacement tags
        return "[REDACTED:HEX_SECRET]"

    text = _LONG_HEX_PATTERN.sub(_replace_hex, text)

    # Redact long base64 secrets
    def _replace_b64(m: re.Match[str]) -> str:
        matched = m.group(0)
        if "REDACTED" in matched:
            return matched
        return "[REDACTED:BASE64_SECRET]"

    text = _LONG_BASE64_PATTERN.sub(_replace_b64, text)

    return text


@dataclass(frozen=True)
class QuarantineResult:
    text: str
    truncated: bool
    notes: list[str]


def quarantine(
    text: str,
    origin: str,
    evidence_id: str | None = None,
    max_chars: int = 10_000,
) -> QuarantineResult:
    """Wrap untrusted text in a safe delimiter envelope and sanitize dangerous content.

    1. Checks for instruction-like phrases (informational flag).
    2. Strips control characters and zero-width unicode formatters.
    3. Normalizes unicode (NFKC) to defeat homoglyph/bidi tricks.
    4. Escapes any occurrence of the closing tag </untrusted_data>.
    5. Truncates to max_chars with a "[truncated N chars]" marker.
    6. Wraps in <untrusted_data origin="..." id="...">...</untrusted_data>.
    """
    if text is None:
        text = ""

    notes: list[str] = detect_instruction_like_text(text)

    # 1. Normalize unicode
    cleaned = unicodedata.normalize("NFKC", text)

    # 2. Strip control characters and zero-width formatting
    cleaned = _CONTROL_AND_ZERO_WIDTH_PATTERN.sub("", cleaned)

    # 3. Escape closing delimiters inside untrusted body
    cleaned = cleaned.replace("</untrusted_data>", "&lt;/untrusted_data&gt;")

    # 4. Truncate if exceeding max_chars
    truncated = False
    if len(cleaned) > max_chars:
        excess = len(cleaned) - max_chars
        cleaned = cleaned[:max_chars] + f"\n[truncated {excess} chars]"
        truncated = True

    # 5. Build envelope attributes
    attrs = f'origin="{origin}"'
    if evidence_id:
        attrs += f' id="{evidence_id}"'

    wrapped = f"<untrusted_data {attrs}>\n{cleaned}\n</untrusted_data>"
    return QuarantineResult(text=wrapped, truncated=truncated, notes=notes)
