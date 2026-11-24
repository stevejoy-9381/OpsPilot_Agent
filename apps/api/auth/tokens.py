"""Cryptographic session token generation and hashing utilities."""

from __future__ import annotations

import hashlib
import secrets


def generate_session_token() -> str:
    """Generates a high-entropy opaque random session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Computes SHA-256 digest of session token to store in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
