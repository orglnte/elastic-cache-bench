"""PROVISIONING SLICE — conventional treatment (rig-debug reference fixture).

Discovery-only. The cache 0<->1 is NOT done here — an external KEDA ScaledObject
scales the declared Redis Deployment 0<->1 on the app's exported pool_pressure.
This slice only: (1) reads the DB connection the platform injected, and (2)
DISCOVERS whether the declared Redis is reachable right now (routing to it when
KEDA has scaled it up, falling back to Postgres when it is at zero).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import redis.asyncio as redis_asyncio

from app.contracts import Backends, CacheClient, LoadSample, Provisioning
from app.settings import Settings

log = logging.getLogger("app.provisioning")

# A terminating Redis pod (KEDA scaling it to 0 under load) can still answer PING
# while GETs hang — so a PING-only liveness check keeps a dead cache "mounted"
# and every read then pays the fallback timeout. The probe therefore exercises
# the actual read path (a GET) under a tight bound and requires it to be fast.
_PROBE_TIMEOUT_S = float(os.environ.get("CACHE_PROBE_TIMEOUT_S", "0.5"))


def _db_dsn() -> str:
    dsn = os.environ.get("DB_DSN")
    if dsn:
        return dsn
    host = os.environ.get("DB_HOST", "postgres")
    port = os.environ.get("DB_PORT", "5432")
    user = os.environ.get("DB_USER", "postgres")
    pw = os.environ.get("DB_PASSWORD", "postgres")
    name = os.environ.get("DB_NAME", "metadata")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


class _RedisCacheClient:
    def __init__(self, host: str, port: int) -> None:
        # Fail-fast timeouts. KEDA scales the Redis Deployment to 0 out-of-band,
        # so the app can be holding this client when the pod vanishes. Without a
        # timeout, both the hot-path reads AND the reachability probe (ping) hang
        # ~20s (until the k6 request timeout) on the now-endpointless Service, so
        # the app can't detect the loss and fall back to Postgres — a cascade of
        # timeouts. A 1s socket timeout lets a read/ping fail fast, so the next
        # on_load_sample probe drops the cache and reads fall back within ~1-2s.
        self._redis = redis_asyncio.Redis(
            host=host, port=int(port), decode_responses=False,
            socket_connect_timeout=1.0, socket_timeout=1.0,
        )

    async def get(self, key: str) -> bytes | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: bytes, ttl_s: int) -> None:
        await self._redis.set(key, value, ex=ttl_s)

    async def ping(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        aclose = getattr(self._redis, "aclose", None)
        if aclose is not None:
            await aclose()
        else:
            await self._redis.close()


class ConventionalProvisioning:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: CacheClient | None = None
        self._cache_host = os.environ.get("CACHE_HOST", "redis")
        self._cache_port = int(os.environ.get("CACHE_PORT", "6379"))
        self._last_probe = 0.0
        self._probe_ttl_s = 1.0  # re-probe cadence; faster = quicker drop when KEDA removes Redis

    async def start(self) -> Backends:
        # Read-only: the DB connection details the manifests injected. The
        # skeleton opens its pool from this DSN.
        return Backends(db_dsn=_db_dsn())

    async def stop(self) -> None:
        if self._cache is not None:
            try:
                await self._cache.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            self._cache = None

    def cache(self) -> CacheClient | None:
        return self._cache

    async def on_load_sample(self, sample: LoadSample) -> None:
        # KEDA scrapes /metrics out-of-process and scales Redis 0<->1. Here we
        # only refresh the reachability probe on a bounded cadence.
        now = time.monotonic()
        if now - self._last_probe < self._probe_ttl_s:
            return
        self._last_probe = now
        await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        probe = _RedisCacheClient(self._cache_host, self._cache_port)
        healthy = False
        try:
            # Exercise the READ path (a GET) under a tight bound, not just PING —
            # a terminating pod answers PING but hangs on GET, so PING alone would
            # keep a dead cache mounted while every read pays the fallback timeout.
            await asyncio.wait_for(probe.get("__cache_health__"), _PROBE_TIMEOUT_S)
            healthy = True
        except Exception:  # noqa: BLE001 - Redis at 0 replicas / not ready / terminating
            healthy = False
        if not healthy:
            try:
                await probe.close()
            except Exception:  # noqa: BLE001
                pass
            if self._cache is not None:
                try:
                    await self._cache.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
                self._cache = None
                log.info("redis unreachable/slow -> cache dropped, reads fall back to db")
            return
        if self._cache is None:
            self._cache = probe
            log.info("redis reachable -> cache mounted")
        else:
            try:
                await probe.close()
            except Exception:  # noqa: BLE001
                pass


def build_provisioning(settings: Settings) -> Provisioning:
    return ConventionalProvisioning(settings)
