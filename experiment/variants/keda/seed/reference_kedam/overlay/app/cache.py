"""Read-through cache for the KEDA ``metrics-api`` treatment: the app decides,
KEDA acts.

* :class:`CacheController` runs the load signals through
  :class:`~app.cache_policy.CachePolicy` once per tick and writes the policy's
  intended state into the metrics dict as ``cache_demand`` (1 = keep a cache,
  0 = none). GET /metrics exports it; the ScaledObject's ``metrics-api``
  trigger reads it and scales the redis Deployment 0<->1. Nothing here
  provisions or scales.
* Discovery: every tick the controller probes redis at
  ``$CACHE_HOST:$CACHE_PORT`` with a GET under a tight bound. Reads go through
  the cache only while that probe answers, and fall back to the store
  otherwise. A terminating pod can answer PING while GETs hang, so the probe
  exercises the read path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from app.cache_policy import CachePolicy, PolicyConfig

log = logging.getLogger("app.cache")

_TTL_S = int(os.environ.get("CACHE_TTL_S", "30"))
_PING_TIMEOUT_S = float(os.environ.get("CACHE_PING_TIMEOUT_S", "0.5"))
_PROBE_TIMEOUT_S = float(os.environ.get("CACHE_PROBE_TIMEOUT_S", "0.5"))
_OP_TIMEOUT_S = float(os.environ.get("CACHE_OP_TIMEOUT_S", "0.25"))


def redis_client_factory():
    """A Redis asyncio client aimed at the address the bring-up forwards the
    redis Service to. Short timeouts: a scaled-away cache fails fast and the
    hot path falls back to the store instead of hanging."""
    import redis.asyncio as aioredis

    return aioredis.Redis(
        host=os.environ.get("CACHE_HOST", "redis"),
        port=int(os.environ.get("CACHE_PORT", "6379")),
        socket_timeout=_OP_TIMEOUT_S,
        socket_connect_timeout=_OP_TIMEOUT_S,
        retry_on_timeout=False,
        decode_responses=True,
    )


class CacheController:
    """Background policy + discovery loop, read-through seam and liveness.
    ``client_factory`` is injectable so the loop runs with fakes in tests."""

    def __init__(
        self,
        metrics: dict,
        *,
        interval_s: float = 1.0,
        policy: CachePolicy | None = None,
        client_factory=redis_client_factory,
    ) -> None:
        self._metrics = metrics
        self._metrics["cache_demand"] = 0
        self._interval_s = interval_s
        self._policy = policy or CachePolicy(PolicyConfig())
        self._client_factory = client_factory

        self._req_count = 0
        self._last_count = 0
        self._last_ts = time.monotonic()

        self._client = None
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        client, self._client = self._client, None
        await _close(client)

    # -- hot-path seam ------------------------------------------------------ #
    def note_request(self) -> None:
        """Called at the start of every hot-path request, shed ones included:
        the offered demand the release is judged on."""
        self._req_count += 1

    async def get(self, key: str):
        """Read-through GET: the decoded value on a hit, else None (miss, no
        cache, or any cache error: the caller reads the store)."""
        client = self._client
        if client is None:
            return None
        try:
            raw = await asyncio.wait_for(client.get(key), timeout=_OP_TIMEOUT_S)
        except Exception as e:
            log.debug("cache get failed (%s); falling back to store", e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def set(self, key: str, value) -> None:
        """Best-effort populate. Never raises into the request path."""
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(client.set(key, json.dumps(value), ex=_TTL_S),
                                   timeout=_OP_TIMEOUT_S)
        except Exception as e:
            log.debug("cache set failed (%s); ignoring", e)

    # -- liveness ----------------------------------------------------------- #
    async def is_mounted(self) -> bool:
        """``cache_mounted``: True only while a cache backend answers a PING."""
        client = self._client
        if client is None:
            return False
        try:
            return bool(await asyncio.wait_for(client.ping(), timeout=_PING_TIMEOUT_S))
        except Exception:
            return False

    # -- control loop ------------------------------------------------------- #
    def _hot_rps(self) -> float:
        now = time.monotonic()
        elapsed = now - self._last_ts
        if elapsed <= 0:
            return 0.0
        delta = self._req_count - self._last_count
        self._last_count = self._req_count
        self._last_ts = now
        return delta / elapsed

    def tick(self, hot_rps: float) -> None:
        """One policy step: the intended state becomes the exported field."""
        self._policy.observe(float(self._metrics.get("pool_pressure", 0.0)),
                             float(self._metrics.get("p99_ms", 0.0)), hot_rps)
        self._metrics["cache_demand"] = 1 if self._policy.mounted else 0

    async def discover(self) -> None:
        """Keep a client only while redis answers a GET under the bound."""
        client = self._client or self._client_factory()
        try:
            await asyncio.wait_for(client.get("__cache_health__"), timeout=_PROBE_TIMEOUT_S)
        except Exception:
            if self._client is not None:
                log.info("redis unreachable/slow -> cache dropped, reads fall back to store")
            self._client = None
            await _close(client)
            return
        if self._client is None:
            log.info("redis reachable -> cache mounted")
            self._client = client

    async def run(self) -> None:
        cfg = self._policy.cfg
        log.info("cache policy started (mount>=%.2f AND lat>=%.0fms AND rps>%.0f x%d, "
                 "release<=%.0frps x%d)", cfg.mount_pressure, cfg.mount_latency_ms,
                 cfg.release_rps, cfg.mount_samples, cfg.release_rps, cfg.release_samples)
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                self.tick(self._hot_rps())
                await self.discover()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("cache loop iteration failed: %s", e)


async def _close(client) -> None:
    if client is None:
        return
    try:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
        else:
            await client.close()
    except Exception:
        pass
