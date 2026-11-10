"""Small async cache: Redis when configured, in-process TTL dict otherwise."""

from __future__ import annotations

import json
import time
from typing import Any

from redis.asyncio import Redis


class Cache:
    def __init__(self, redis: Redis | None = None, prefix: str = "xmonitor:") -> None:
        self._redis = redis
        self._prefix = prefix
        self._local: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> Any | None:
        key = self._prefix + key
        if self._redis is not None:
            raw = await self._redis.get(key)
        else:
            item = self._local.get(key)
            raw = item[1] if item and item[0] > time.monotonic() else None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        key = self._prefix + key
        raw = json.dumps(value)
        if self._redis is not None:
            await self._redis.set(key, raw, ex=ttl)
        else:
            self._local[key] = (time.monotonic() + ttl, raw)
