"""FIXED SKELETON — do not edit.

Transactional-store access: an async psycopg pool opened from the DSN the
provisioning slice resolved, plus the per-task schema. The read paths run a
deliberately CPU-heavy query (a ts_rank over a synthetic corpus) so that a
load spike saturates the DB and the pool-pressure signal climbs — that is
what makes the cache 0<->1 event observable. This is identical across both
variants; only who provisions the DB differs.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("app.db")

# Synthetic-corpus size for the per-request CPU burn. Sized so one query
# costs enough that a modest spike saturates a small pool and pool_pressure
# rises above the scaler threshold. Tune via env if your rig is faster.
import os

_QUERY_CPU_OPS = int(os.environ.get("QUERY_CPU_OPS", "20000"))

# Per-task DDL. Kept tiny — the point is a hot read path over Postgres, not a
# rich schema.
_SCHEMA = {
    "T1": [
        """CREATE TABLE IF NOT EXISTS photos (
             id TEXT PRIMARY KEY,
             owner TEXT NOT NULL,
             caption TEXT NOT NULL DEFAULT '',
             created_at TIMESTAMPTZ NOT NULL DEFAULT now()
           )""",
        "CREATE INDEX IF NOT EXISTS photos_owner_idx ON photos (owner)",
    ],
}


async def open_pool(dsn: str, *, min_size: int | None = None, max_size: int = int(os.environ.get("POOL_MAX", "16"))) -> AsyncConnectionPool:
    # WARM POOL: pre-open the FULL pool (min_size = max_size) so the measured load
    # never pays cold connection-growth. Growing 2->16 on demand takes seconds
    # when each connect crosses kind's NAT (conv_keda: pod -> kube-proxy -> host
    # -> store), which queues requests and spikes pool_pressure at BASELINE before
    # the spike — a cold-start artifact that trips the scaler prematurely. Opening
    # the whole pool up front moves that cost into startup, behind the readiness
    # gate, matching the warm pool the rig simulates. Identical for both variants.
    if min_size is None:
        min_size = max_size
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
    await pool.open(wait=True, timeout=30)
    log.info("db pool opened warm (min=%d max=%d)", min_size, max_size)
    return pool


async def migrate(pool: AsyncConnectionPool, task: str) -> None:
    stmts = _SCHEMA.get(task.upper())
    if not stmts:
        raise ValueError(f"no schema for task {task!r} (expected T1)")
    async with pool.connection() as conn:
        for stmt in stmts:
            await conn.execute(stmt)
        await conn.commit()
    log.info("migrations applied for %s (%d statements)", task, len(stmts))


async def burn_and_rank(cur, seed: str) -> None:
    """The CPU-heavy step shared by every hot read path. Materialises a
    synthetic corpus and scores it with ts_rank — honest CPU work (no I/O
    wait) so DB saturation, not connection artefacts, drives pool pressure."""
    await cur.execute(
        "SELECT count(*) FROM ("
        "  SELECT ts_rank("
        "    to_tsvector('english', md5(i::text || %s)),"
        "    plainto_tsquery('english', %s)"
        "  ) AS r FROM generate_series(1, %s) AS s(i)"
        ") ranked WHERE r >= 0",
        (seed, seed, _QUERY_CPU_OPS),
    )
    await cur.fetchone()
