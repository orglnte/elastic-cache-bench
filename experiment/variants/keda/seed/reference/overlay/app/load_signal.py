"""FIXED SKELETON — do not edit.

The load-signal surface. Two consumers of the SAME pool-pressure number:

  1. GET /metrics  — JSON the EXTERNAL autoscaler reads (the conventional
     treatment points a KEDA metrics-api trigger / an HPA custom metric at
     this endpoint). Exposed unconditionally; harmless for in-code policies.
  2. on_load_sample() — pushed to the provisioning slice on a bounded
     cadence (SETTINGS.load_sample_interval_s), NOT per request. An in-code
     slice drives its scaler from these; the conventional slice may
     ignore them (its scaler reads /metrics out-of-process).

pool_pressure = requests_waiting / max_pool_size, clamped to [0, 1]. This is
the causal saturation signal: it climbs the instant the pool backs up, ahead
of latency or error rate.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from app.admission import Admission
from app.contracts import LoadSample, Provisioning

log = logging.getLogger("app.load_signal")


def pool_pressure(pool: AsyncConnectionPool) -> float:
    stats = pool.get_stats()
    waiting = stats.get("requests_waiting", 0)
    max_size = stats.get("pool_max", stats.get("pool_size", 1)) or 1
    return max(0.0, min(1.0, waiting / max_size))


def sample(pool: AsyncConnectionPool, admission: Admission) -> LoadSample:
    return LoadSample(
        pool_pressure=pool_pressure(pool),
        inflight=admission.inflight,
        rps=admission.rps(),
        p99_ms=admission.p99(),
    )


async def run_loop(
    pool: AsyncConnectionPool,
    admission: Admission,
    prov: Provisioning,
    interval_s: float,
    *,
    latest: dict,
) -> None:
    """Background task: sample on a bounded cadence, update `latest` (read by
    GET /metrics), and push to the provisioning slice. Runs until cancelled."""
    while True:
        try:
            s = sample(pool, admission)
            latest["pool_pressure"] = s.pool_pressure
            latest["inflight"] = s.inflight
            latest["rps"] = s.rps
            latest["p99_ms"] = s.p99_ms
            await prov.on_load_sample(s)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("load-signal loop iteration failed: %s", e)
        await asyncio.sleep(interval_s)
