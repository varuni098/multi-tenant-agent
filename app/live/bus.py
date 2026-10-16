"""Per-tenant live event channel: Redis pub/sub in deployments, in-process queues for tests and local runs.

The live bridge publishes committed tenant changes here; the API's /v1/events stream subscribes,
so open UIs see a theme change the moment it commits, whoever made it.
"""

import json
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import get_settings


def channel(tenant_id: str) -> str:
    return f"live:{tenant_id}"


class Subscription(Protocol):
    def get(self, timeout: float) -> dict[str, Any] | None:
        """Next event, or None after `timeout` seconds without one."""
        ...


class Bus(Protocol):
    def publish(self, tenant_id: str, event: dict[str, Any]) -> None: ...
    def subscribe(self, tenant_id: str) -> Any:
        """Context manager yielding a Subscription."""
        ...


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def publish(self, tenant_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(tenant_id, []))
        for q in subs:
            q.put(event)

    @contextmanager
    def subscribe(self, tenant_id: str) -> Iterator[Subscription]:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subs.setdefault(tenant_id, []).append(q)
        try:
            yield _QueueSubscription(q)
        finally:
            with self._lock:
                self._subs[tenant_id].remove(q)


class _QueueSubscription:
    def __init__(self, q: queue.Queue[dict[str, Any]]) -> None:
        self._q = q

    def get(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class RedisBus:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def publish(self, tenant_id: str, event: dict[str, Any]) -> None:
        self._redis.publish(channel(tenant_id), json.dumps(event, default=str))

    @contextmanager
    def subscribe(self, tenant_id: str) -> Iterator[Subscription]:
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel(tenant_id))
        try:
            yield _RedisSubscription(pubsub)
        finally:
            pubsub.close()


class _RedisSubscription:
    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    def get(self, timeout: float) -> dict[str, Any] | None:
        message = self._pubsub.get_message(timeout=timeout)
        if not message or message.get("type") != "message":
            return None
        try:
            return json.loads(message["data"])
        except ValueError:
            return None


@lru_cache
def get_bus() -> Bus:
    url = get_settings().redis_url
    return RedisBus(url) if url else InMemoryBus()
