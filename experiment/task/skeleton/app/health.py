"""GET /health  — liveness + readiness. 200 only once the store answers.
GET /metrics — the exported load signals (JSON, cheap, no auth).

/health is the deploy contract every treatment declares (see each
bringup/*/deploy.sh), and the rig gates the start of a verify on it holding.
It therefore has to mean "this service can actually serve", not "this process
is running": a 200 from a service whose store is unreachable turns a rig fault
into a failed attempt scored against the authored code.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def make_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> JSONResponse:
        st = request.app.state
        pool = getattr(st, "pool", None)
        pool_stats = pool.get_stats() if pool else {}

        # Round-trip the store rather than trusting the pool object's presence:
        # a pool can exist while the backend refuses connections, and that is
        # precisely the state this endpoint must not report as healthy.
        store_ok, store_err = False, "no pool"
        if pool is not None:
            try:
                async with pool.connection() as conn:
                    cur = await conn.execute("SELECT 1")
                    await cur.fetchone()
                store_ok, store_err = True, ""
            except Exception as e:                      # noqa: BLE001 — any failure is unhealthy
                store_err = f"{type(e).__name__}: {e}"[:200]

        body = {
            "status": "ok" if store_ok else "unavailable",
            "task": st.task,
            "store": "ok" if store_ok else store_err,
            "pool_pressure": st.metrics.get("pool_pressure", 0.0),
            "p99_ms": st.metrics.get("p99_ms", 0.0),
            "rps": st.metrics.get("rps", 0.0),
            "inflight": st.metrics.get("inflight", 0),
            "pool_size": pool_stats.get("pool_size", 0),
            "pool_waiting": pool_stats.get("requests_waiting", 0),
        }
        return JSONResponse(body, status_code=200 if store_ok else 503)

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
