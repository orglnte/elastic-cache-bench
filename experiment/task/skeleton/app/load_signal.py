"""The load-signal surface.

Samples the pool-pressure saturation signal on a bounded cadence
(SETTINGS.load_sample_interval_s, NOT per request) and updates the metrics
dict exported at GET /metrics.

pool_pressure = requests_waiting / max_pool_size, clamped to [0, 1]. This is
the causal saturation signal: it climbs the instant the pool backs up, ahead
of latency or error rate.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from app.admission import Admission

log = logging.getLogger("app.load_signal")


def pool_pressure(pool: AsyncConnectionPool) -> float:
    stats = pool.get_stats()
    waiting = stats.get("requests_waiting", 0)
    max_size = stats.get("pool_max", stats.get("pool_size", 1)) or 1
    return max(0.0, min(1.0, waiting / max_size))


async def run_loop(
    pool: AsyncConnectionPool,
    admission: Admission,
    interval_s: float,
    *,
    latest: dict,
) -> None:
    """Background task: sample on a bounded cadence and update `latest` (read
    by GET /metrics and GET /health). Runs until cancelled."""
    while True:
        try:
            latest["pool_pressure"] = pool_pressure(pool)
            latest["inflight"] = admission.inflight
            latest["rps"] = admission.rps()
            latest["p99_ms"] = admission.p99()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("load-signal loop iteration failed: %s", e)
        await asyncio.sleep(interval_s)
