"""FIXED SKELETON — do not edit.

THE PROVISIONING SEAM.

This module defines the contract between the fixed application skeleton
and the one file the coding agent authors (app/provisioning_impl.py).
Everything above the seam (routes, DB access, the read-through cache, the
load signal, health) depends ONLY on the `Provisioning` protocol below —
it never imports a provisioning SDK, never reads a Terraform output, never talks
to Kubernetes. That is what keeps the two variants comparable: the app
is byte-identical; only the implementation behind this seam differs.

The provisioning slice the agent must deliver:

  * `start()`  — return the injected DSN of the FIXED, harness-owned
                 transactional store (env `DB_DSN`); the agent does NOT
                 provision the store. Also stand up whatever runtime machinery
                 scales the cache ON DEMAND. Returns a `Backends` the skeleton
                 opens its pool from.
  * `stop()`   — release everything provisioned in `start()`.
  * `cache()`  — the CURRENT cache client, or None when no cache is
                 mounted right now. Called on the hot read path. Returning
                 None is normal (before first acquire, after release, or
                 while a requested scale-up has not landed) — the skeleton
                 falls back to the transactional store.
  * `on_load_sample()` — the skeleton pushes a load snapshot here on a
                 bounded cadence. What the slice DOES with it is the
                 treatment's whole story:
                   - in-code: decide + acquire/release the cache in-code.
                   - conventional: publish the load metric the external
                     autoscaler (KEDA/HPA) reads; the cache 0<->1 happens
                     out-of-process; `cache()` then discovers whether the
                     declared cache is currently reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Backends:
    """Connection details the provisioning slice resolved for the startup
    resources. The skeleton opens its Postgres pool from `db_dsn`; it never
    constructs a DSN from raw env itself."""

    # libpq DSN / URL for the transactional store, e.g.
    # "postgresql://user:pass@host:5432/dbname".
    db_dsn: str


@dataclass(frozen=True)
class LoadSample:
    """One load snapshot the skeleton emits on a bounded cadence. The
    provisioning slice consumes these to drive (in-code) or publish
    (conventional) the cache scaling decision."""

    # Pool pressure in [0, 1]: waiting-for-a-connection depth over pool
    # capacity. The canonical causal saturation signal — the same value
    # the skeleton exposes at GET /metrics for an external autoscaler.
    pool_pressure: float
    # In-flight requests admitted past the admission gate right now.
    inflight: int
    # Requests/sec observed over the last sampling window (TRAFFIC — the one
    # golden signal a working cache does NOT mask, so the release signal).
    rps: float
    # Served-latency p99 (ms) over the last window (LATENCY — the SLO golden
    # signal, ground truth that the store is failing its contract; the acquire
    # signal, gated by pool_pressure/saturation as the leading indicator).
    p99_ms: float = 0.0


@runtime_checkable
class CacheClient(Protocol):
    """Minimal read-through cache surface the skeleton uses. Wrap Redis (or
    the ephemeral-kv resource) behind this. `get`/`set` operate on bytes;
    the skeleton handles (de)serialization."""

    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, ttl_s: int) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class Provisioning(Protocol):
    """The provisioning slice under test. app/provisioning_impl.py must
    provide `build_provisioning(settings) -> Provisioning`."""

    async def start(self) -> Backends: ...

    async def stop(self) -> None: ...

    def cache(self) -> CacheClient | None: ...

    async def on_load_sample(self, sample: LoadSample) -> None: ...
