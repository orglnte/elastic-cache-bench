"""Runtime, read-through cache provisioning — in-service, via the Pulumi
Automation API.

Three responsibilities, deliberately layered so the decision logic stays
testable without a Docker daemon (see ``app/cache_policy.py``):

* :class:`PulumiProvisioner` — the only place that touches Pulumi. Runs an
  inline ``pulumi_docker`` program against ``$PULUMI_DOCKER_HOST`` to bring a
  Redis container up / tear it down. ``up()``/``destroy()`` are blocking engine
  runs (seconds), so the controller drives them off the request path in a
  worker thread.
* :class:`CacheController` — the background control loop. Samples the load
  signals, runs them through :class:`~app.cache_policy.CachePolicy`, and
  effects the resulting mount/release. Owns the current cache client and the
  truthful ``cache_mounted`` liveness answer, and exposes the read-through
  ``get``/``set`` seam the hot path uses.
* :func:`redis_client_factory` — builds the Redis client aimed at
  ``$PULUMI_CACHE_HOST:$PULUMI_CACHE_PORT`` (where the substrate maps the
  container's published ``6379``).

``docs/pulumi.md``/`docs/project.md` invariants honoured here: single owner of
the stack (no concurrent ``up()``), route around a just-destroyed cache
(availability flipped *before* ``destroy()``), and never trust connect-success
for liveness — only a real ``PING`` reply counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from app.cache_policy import CachePolicy, PolicyConfig, Decision

log = logging.getLogger("app.cache")

STACK_NAME = os.environ.get("CACHE_STACK_NAME", "cache")
PROJECT_NAME = os.environ.get("CACHE_PROJECT_NAME", "photoshare-cache")
CACHE_IMAGE = os.environ.get("CACHE_IMAGE", "redis:7-alpine")

_TTL_S = int(os.environ.get("CACHE_TTL_S", "30"))
_PING_TIMEOUT_S = float(os.environ.get("CACHE_PING_TIMEOUT_S", "0.5"))
_OP_TIMEOUT_S = float(os.environ.get("CACHE_OP_TIMEOUT_S", "0.25"))


# --------------------------------------------------------------------------- #
# Redis client
# --------------------------------------------------------------------------- #
def redis_client_factory():
    """Build a Redis asyncio client aimed at the substrate-mapped cache
    address. Short timeouts so a stale/torn-down endpoint fails fast and the
    hot path falls back to Postgres instead of hanging."""
    import redis.asyncio as aioredis

    host = os.environ.get("PULUMI_CACHE_HOST", "127.0.0.1")
    port = int(os.environ.get("PULUMI_CACHE_PORT", "6379"))
    return aioredis.Redis(
        host=host,
        port=port,
        socket_timeout=_OP_TIMEOUT_S,
        socket_connect_timeout=_OP_TIMEOUT_S,
        retry_on_timeout=False,
        decode_responses=True,
    )


# --------------------------------------------------------------------------- #
# Pulumi provisioner  (the only Pulumi-touching code)
# --------------------------------------------------------------------------- #
def _cache_program() -> None:
    """Inline Pulumi program: a single Redis container on the project Docker
    endpoint, publishing ``6379`` so the substrate mapping to
    ``$PULUMI_CACHE_HOST:$PULUMI_CACHE_PORT`` holds."""
    import os as _os

    import pulumi
    import pulumi_docker as docker

    provider = docker.Provider("dind", host=_os.environ["PULUMI_DOCKER_HOST"])
    opts = pulumi.ResourceOptions(provider=provider)

    image = docker.RemoteImage(
        "cache-image",
        name=CACHE_IMAGE,
        keep_locally=True,
        opts=opts,
    )
    docker.Container(
        "cache",
        image=image.image_id,
        # No fixed container name: let Docker/Pulumi assign a unique one so a
        # re-mount can never collide with a lingering name. Reachability is via
        # the published port mapping, not the container name.
        ports=[docker.ContainerPortArgs(internal=6379, external=6379)],
        restart="unless-stopped",
        opts=opts,
    )


class PulumiProvisioner:
    """Thin, blocking wrapper over one Automation-API stack. ``up`` and
    ``destroy`` are synchronous (engine runs); the controller calls them via
    ``asyncio.to_thread`` so the event loop keeps serving."""

    def __init__(self) -> None:
        self._stack = None
        self._lock = threading.Lock()

    def _get_stack(self):
        # Same stack name every time: create_or_select is idempotent, so
        # up-after-up is a no-op refresh and up-after-destroy re-provisions
        # cleanly from the persisted state.
        if self._stack is None:
            from pulumi import automation as auto

            self._stack = auto.create_or_select_stack(
                stack_name=STACK_NAME,
                project_name=PROJECT_NAME,
                program=_cache_program,
            )
        return self._stack

    def up(self) -> None:
        with self._lock:
            stack = self._get_stack()
            stack.up(on_output=lambda m: log.info("pulumi up: %s", m.rstrip()))

    def destroy(self) -> None:
        with self._lock:
            stack = self._get_stack()
            stack.destroy(on_output=lambda m: log.info("pulumi destroy: %s", m.rstrip()))


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class CacheController:
    """Background control loop + read-through seam + liveness.

    Injected ``provisioner`` and ``client_factory`` keep the loop unit-testable
    with fakes (no Docker, no Redis).
    """

    def __init__(
        self,
        metrics: dict,
        *,
        interval_s: float = 1.0,
        policy: CachePolicy | None = None,
        provisioner=None,
        client_factory=redis_client_factory,
    ) -> None:
        self._metrics = metrics
        self._interval_s = interval_s
        self._policy = policy or CachePolicy(PolicyConfig())
        self._provisioner = provisioner if provisioner is not None else PulumiProvisioner()
        self._client_factory = client_factory

        # Hot-path arrival accounting (the demand signal that drives release).
        self._req_count = 0
        self._last_count = 0
        self._last_ts = time.monotonic()

        # Current cache handle + availability flag. ``available`` is the fast,
        # per-request routing gate; it is flipped False *before* a destroy so
        # in-flight reads route around the disappearing cache.
        self._client = None
        self._available = False
        self._busy = False  # a mount/release transition is in progress

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
        # Best-effort release so we do not leave an orphan container behind
        # when this process exits mid-run.
        if self._available or self._client is not None:
            try:
                await self._release()
            except Exception as e:  # pragma: no cover - shutdown best effort
                log.warning("cache release on shutdown failed: %s", e)

    # -- hot-path seam ------------------------------------------------------ #
    def note_request(self) -> None:
        """Called at the start of every hot-path request (arrivals, including
        shed ones): this is the offered-demand signal used for release."""
        self._req_count += 1

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def get(self, key: str):
        """Read-through GET. Returns the decoded value on a hit, else None
        (miss, unavailable, or any cache error -> caller falls back to DB)."""
        if not self.available:
            return None
        client = self._client
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
        if not self.available:
            return
        client = self._client
        try:
            await asyncio.wait_for(
                client.set(key, json.dumps(value), ex=_TTL_S), timeout=_OP_TIMEOUT_S
            )
        except Exception as e:
            log.debug("cache set failed (%s); ignoring", e)

    # -- liveness ----------------------------------------------------------- #
    async def is_mounted(self) -> bool:
        """Truthful ``cache_mounted``: True only while a cache backend actually
        exists AND answers. A live PING reply is the only accepted proof — a
        bare TCP connect can succeed against a torn-down mapping and then close
        empty (see docs), so connect-success is never trusted."""
        client = self._client
        if client is None or not self._available:
            return False
        try:
            pong = await asyncio.wait_for(client.ping(), timeout=_PING_TIMEOUT_S)
            return bool(pong)
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

    async def run(self) -> None:
        log.info(
            "cache controller started (mount>=%.2f AND lat>=%.0fms x%d, release<=%.0frps x%d)",
            self._policy.cfg.mount_pressure,
            self._policy.cfg.mount_latency_ms,
            self._policy.cfg.mount_samples,
            self._policy.cfg.release_rps,
            self._policy.cfg.release_samples,
        )
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                hot_rps = self._hot_rps()
                if self._busy:
                    # A mount/release is running in a worker thread; do not
                    # decide (or churn the streaks) mid-transition.
                    continue
                pressure = float(self._metrics.get("pool_pressure", 0.0))
                p99_ms = float(self._metrics.get("p99_ms", 0.0))
                decision = self._policy.observe(pressure, p99_ms, hot_rps)
                if decision is Decision.MOUNT:
                    self._busy = True
                    asyncio.create_task(self._guarded(self._mount(), intended=True))
                elif decision is Decision.RELEASE:
                    self._busy = True
                    asyncio.create_task(self._guarded(self._release(), intended=False))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive no matter what
                log.warning("cache control iteration failed: %s", e)

    async def _guarded(self, coro, *, intended: bool) -> None:
        try:
            await coro
        except Exception as e:
            log.warning("cache transition failed: %s", e)
            # Roll the policy's intended state back so the condition retries.
            self._policy.revert(intended)
        finally:
            self._busy = False

    async def _mount(self) -> None:
        log.info("sustained saturation -> mounting cache")
        await asyncio.to_thread(self._provisioner.up)
        client = self._client_factory()
        # Only declare the cache usable once it actually answers.
        try:
            pong = await asyncio.wait_for(client.ping(), timeout=_PING_TIMEOUT_S)
        except Exception:
            pong = False
        if not pong:
            # Give the container a brief moment to accept connections.
            for _ in range(20):
                await asyncio.sleep(0.25)
                try:
                    if await asyncio.wait_for(client.ping(), timeout=_PING_TIMEOUT_S):
                        pong = True
                        break
                except Exception:
                    continue
        if not pong:
            await self._close_client(client)
            raise RuntimeError("cache mounted but never answered PING")
        self._client = client
        self._available = True
        log.info("cache mounted and answering")

    async def _release(self) -> None:
        log.info("demand drained -> releasing cache")
        # Route around the cache FIRST so no request touches it during teardown.
        self._available = False
        client, self._client = self._client, None
        await self._close_client(client)
        await asyncio.to_thread(self._provisioner.destroy)
        log.info("cache released; reads fall back to store")

    async def _close_client(self, client) -> None:
        if client is None:
            return
        try:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - older redis
                await client.close()
        except Exception:
            pass
