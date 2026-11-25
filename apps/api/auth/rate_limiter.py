"""Login rate limiting with Redis backend and in-memory fallback.

Prevents brute force attacks by tracking consecutive failed attempts per IP / identifier.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from threading import Lock
from typing import Any

import redis

logger = logging.getLogger(__name__)

# Default rate limit: 5 attempts per 60 seconds
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_WINDOW_SECONDS = 60


class RateLimiter:
    """Rate limiter supporting Redis with thread-safe in-memory fallback."""

    def __init__(
        self,
        redis_url: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._redis_client: redis.Redis[Any] | None = None
        self._use_redis = False
        self._lock = Lock()
        self._memory_store: dict[str, list[float]] = defaultdict(list)

        url: str = redis_url or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        try:
            client = redis.Redis.from_url(url, socket_connect_timeout=1)
            client.ping()
            self._redis_client = client
            self._use_redis = True
            logger.info("RateLimiter initialized with Redis backend at %s", url)
        except Exception:
            logger.info("Redis not available; RateLimiter using thread-safe in-memory storage")

    def is_rate_limited(self, key: str) -> bool:
        """Returns True if the rate limit is exceeded for the key, else False."""
        now = time.time()
        if self._use_redis and self._redis_client is not None:
            try:
                redis_key = f"rl:{key}"
                pipe = self._redis_client.pipeline()
                pipe.zremrangebyscore(redis_key, 0, now - self.window_seconds)
                pipe.zcard(redis_key)
                _, count = pipe.execute()
                return bool(count >= self.max_attempts)
            except Exception as e:
                logger.warning("Redis rate limit check error: %s; falling back to memory", e)

        # In-memory rate limiting
        with self._lock:
            cutoff = now - self.window_seconds
            timestamps = [ts for ts in self._memory_store[key] if ts > cutoff]
            self._memory_store[key] = timestamps
            return len(timestamps) >= self.max_attempts

    def record_attempt(self, key: str) -> None:
        """Records an attempt for the key."""
        now = time.time()
        if self._use_redis and self._redis_client is not None:
            try:
                redis_key = f"rl:{key}"
                pipe = self._redis_client.pipeline()
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, self.window_seconds)
                pipe.execute()
                return
            except Exception as e:
                logger.warning("Redis record attempt error: %s; falling back to memory", e)

        with self._lock:
            self._memory_store[key].append(now)

    def reset(self, key: str) -> None:
        """Resets the rate limit counter for the key (e.g. after successful login)."""
        if self._use_redis and self._redis_client is not None:
            try:
                self._redis_client.delete(f"rl:{key}")
            except Exception:
                pass
        with self._lock:
            self._memory_store.pop(key, None)


# Global rate limiter instance
login_rate_limiter = RateLimiter()
