"""GET /health  — liveness + readiness (200 once the pool is open).
GET /metrics — the exported load signals (JSON, cheap, no auth).
"""

from __future__ import annotations

from fastapi import APIRouter, Request


def make_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict:
        st = request.app.state
        pool_stats = st.pool.get_stats() if getattr(st, "pool", None) else {}
        cache = getattr(st, "cache", None)
        # Truthful: True only while a cache backend actually exists and answers
        # a live PING (see docs/project.md), False otherwise (incl. at idle).
        cache_mounted = await cache.is_mounted() if cache is not None else False
        return {
            "status": "ok",
            "task": st.task,
            "cache_mounted": cache_mounted,
            "pool_pressure": st.metrics.get("pool_pressure", 0.0),
            "p99_ms": st.metrics.get("p99_ms", 0.0),
            "rps": st.metrics.get("rps", 0.0),
            "inflight": st.metrics.get("inflight", 0),
            "pool_size": pool_stats.get("pool_size", 0),
            "pool_waiting": pool_stats.get("requests_waiting", 0),
        }

    @router.get("/metrics")
    async def metrics(request: Request) -> dict:
        m = request.app.state.metrics
        return {
            "pool_pressure": m.get("pool_pressure", 0.0),
            "p99_ms": m.get("p99_ms", 0.0),
            "rps": m.get("rps", 0.0),
            "inflight": m.get("inflight", 0),
        }

    return router
