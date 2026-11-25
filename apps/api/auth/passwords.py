"""Password hashing and verification utilities using Argon2.

Uses argon2-cffi (argon2.PasswordHasher) with constant-time verification.
"""

from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

# Standard production-ready Argon2id settings
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,  # 64 MB
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hashes a plaintext password using Argon2id."""
    return str(_hasher.hash(password))


def verify_password(password_hash: str, password: str) -> bool:
    """Verifies a plaintext password against an Argon2id hash in constant time."""
    try:
        return bool(_hasher.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError):
        return False
    except Exception as e:
        logger.error("Unexpected error during password verification: %s", type(e).__name__)
        return False
