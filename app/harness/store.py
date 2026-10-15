"""Small key-value store for harness state: Redis in deployments, in-memory for tests and local runs."""

import threading
import time
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings


class Store(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int) -> None: ...
    def pop(self, key: str) -> str | None:
        """Atomically get and delete (single-use values)."""
        ...

    def incr(self, key: str, ttl: int) -> int:
        """Increment a counter; the TTL starts on first increment."""
        ...


class InMemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] < time.monotonic():
            del self._data[key]
            return None
        return item[0]

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._live(key)

    def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)

    def pop(self, key: str) -> str | None:
        with self._lock:
            value = self._live(key)
            self._data.pop(key, None)
            return value

    def incr(self, key: str, ttl: int) -> int:
        with self._lock:
            current = self._live(key)
            if current is None:
                self._data[key] = ("1", time.monotonic() + ttl)
                return 1
            value = int(current) + 1
            self._data[key] = (str(value), self._data[key][1])
            return value


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._redis.get(key)  # type: ignore[return-value]

    def set(self, key: str, value: str, ttl: int) -> None:
        self._redis.set(key, value, ex=ttl)

    def pop(self, key: str) -> str | None:
        return self._redis.getdel(key)  # type: ignore[return-value]

    def incr(self, key: str, ttl: int) -> int:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        return int(pipe.execute()[0])


@lru_cache
def get_store() -> Store:
    url = get_settings().redis_url
    return RedisStore(url) if url else InMemoryStore()
