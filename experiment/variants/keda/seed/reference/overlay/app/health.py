"""FIXED SKELETON — do not edit.

GET /health  — liveness + readiness (200 once the pool is open and startup
                resources are provisioned). deploy.sh + verify.sh poll this.
GET /metrics — the exported load metric the external autoscaler reads
                (conventional treatment). JSON, cheap, no auth.
"""

from __future__ import annotations

from fastapi import APIRouter, Request


def make_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict:
        st = request.app.state
        pool_stats = st.pool.get_stats() if getattr(st, "pool", None) else {}
        return {
            "status": "ok",
            "task": st.task,
            # cache_mounted reflects the CURRENT provisioning reality: True
            # only when the slice reports a live cache client right now.
            "cache_mounted": st.prov.cache() is not None,
            # The provisioning slice's DECISION state, if it exposes one (e.g.
            # idle/acquiring/acquired/releasing) — lets the trace show WHEN the
            # scaler decided, separate from when the resource actually mounted.
            "scaler_state": st.prov.state() if hasattr(st.prov, "state") else "n/a",
            "pool_pressure": st.metrics.get("pool_pressure", 0.0),
            "p99_ms": st.metrics.get("p99_ms", 0.0),
            "rps": st.metrics.get("rps", 0.0),
            "inflight": st.metrics.get("inflight", 0),
            "pool_size": pool_stats.get("pool_size", 0),
            "pool_waiting": pool_stats.get("requests_waiting", 0),
        }

    @router.get("/metrics")
    async def metrics(request: Request) -> dict:
        # The external autoscaler (KEDA metrics-api trigger / HPA custom
        # metric) scrapes this. `pool_pressure` is the scale target.
        m = request.app.state.metrics
        return {
            "pool_pressure": m.get("pool_pressure", 0.0),
            "p99_ms": m.get("p99_ms", 0.0),
            "rps": m.get("rps", 0.0),
            "inflight": m.get("inflight", 0),
        }

    return router
