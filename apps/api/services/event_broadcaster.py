"""Event fan-out broadcaster using Redis pub/sub with thread-safe in-process fallback.

Enforces:
1. Redis pub/sub waking of async SSE streams.
2. Authoritative source of truth remains the PostgreSQL database (app.run_events).
3. Thread-safe in-process fallback when Redis is absent or during tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from uuid import UUID

import redis
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class EventBroadcaster:
    """Broadcaster managing event fan-out notifications."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url: str = redis_url or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
        self._sync_redis: Any = None
        self._listeners: dict[str, set[asyncio.Event]] = {}
        try:
            r = redis.Redis.from_url(self.redis_url, socket_connect_timeout=1)
            r.ping()
            self._sync_redis = r
        except Exception:
            self._sync_redis = None

    def publish_event(self, run_id: UUID | str, seq: int) -> None:
        """Publishes lightweight wakeup notification for a run event."""
        run_key = str(run_id)
        # 1. Notify Redis pub/sub channel if available
        if self._sync_redis is not None:
            try:
                channel = f"run_events:{run_key}"
                self._sync_redis.publish(
                    channel,
                    json.dumps({"run_id": run_key, "seq": seq}),
                )
            except Exception as e:
                logger.warning("Redis pub/sub publish error: %s", e)

        # 2. Notify any registered in-process async listeners
        events = self._listeners.get(run_key, set())
        for ev in list(events):
            ev.set()

    async def wait_for_event(self, run_id: UUID | str, timeout: float = 15.0) -> bool:
        """Waits up to timeout seconds for a new event on run_id.

        Returns True if an event was signaled, False if timeout expired (heartbeat).
        """
        run_key = str(run_id)
        pubsub = None
        redis_client = None

        # Attempt to subscribe to Redis pub/sub
        try:
            redis_client = aioredis.from_url(self.redis_url, socket_connect_timeout=1)
            await redis_client.ping()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(f"run_events:{run_key}")
        except Exception:
            if pubsub:
                await pubsub.close()
            if redis_client:
                await redis_client.close()
            pubsub = None
            redis_client = None

        # Register in-process asyncio event for local/fallback wakeup
        local_ev = asyncio.Event()
        if run_key not in self._listeners:
            self._listeners[run_key] = set()
        self._listeners[run_key].add(local_ev)

        try:
            if pubsub is not None:
                start = asyncio.get_running_loop().time()
                while True:
                    elapsed = asyncio.get_running_loop().time() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        return False
                    if local_ev.is_set():
                        local_ev.clear()
                        return True
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=min(remaining, 1.0),
                    )
                    if msg is not None:
                        return True
            else:
                try:
                    await asyncio.wait_for(local_ev.wait(), timeout=timeout)
                    local_ev.clear()
                    return True
                except asyncio.TimeoutError:
                    return False
        finally:
            self._listeners.get(run_key, set()).discard(local_ev)
            if not self._listeners.get(run_key):
                self._listeners.pop(run_key, None)
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(f"run_events:{run_key}")
                    await pubsub.close()
                except Exception:
                    pass
            if redis_client is not None:
                try:
                    await redis_client.close()
                except Exception:
                    pass


# Global singleton instance
broadcaster = EventBroadcaster()
