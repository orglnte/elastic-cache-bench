"""Reference solution (rig validation only — never agent-visible).

Discovery-only cache access, conventional treatment: the cache 0<->1 happens
OUT of process (KEDA scales the declared Redis Deployment on the exported
signals); this code only discovers whether the declared cache is reachable
right now at CACHE_HOST:CACHE_PORT. Reachability is an answered round-trip
(ping), never client construction.
"""

from __future__ import annotations

import asyncio
import logging
import os

from redis.asyncio import from_url

log = logging.getLogger("app.provisioning")

_PING_TIMEOUT_S = 1.0


class CacheDiscovery:
    def __init__(self) -> None:
        self._host = os.environ.get("CACHE_HOST", "127.0.0.1")
        self._port = int(os.environ.get("CACHE_PORT", "6379"))
        self._redis = None

    @property
    def cache(self):
        """The current cache client, or None (reads then go to Postgres)."""
        return self._redis

    @property
    def mounted(self) -> bool:
        return self._redis is not None

    async def refresh(self) -> None:
        """Called ~1/s: probe reachability, attach/detach the client."""
        if self._redis is None:
            r = from_url(
                f"redis://{self._host}:{self._port}/0",
                decode_responses=False,
                socket_connect_timeout=0.5,
                socket_timeout=0.8,
            )
            try:
                await asyncio.wait_for(r.ping(), _PING_TIMEOUT_S)
                self._redis = r
                log.info("cache discovered at %s:%s", self._host, self._port)
            except Exception:
                try:
                    await r.aclose()
                except Exception:
                    pass
        else:
            try:
                await asyncio.wait_for(self._redis.ping(), _PING_TIMEOUT_S)
            except Exception:
                redis, self._redis = self._redis, None
                try:
                    await redis.aclose()
                except Exception:
                    pass
                log.info("cache gone (scaled to zero) — falling back to the store")

    async def stop(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
