"""Application wiring.

Lifespan: open the Postgres pool (DSN from $DB_DSN), migrate, start the
load-signal loop (samples pool pressure on a bounded cadence, updates the
metrics exported at GET /metrics), serve. On shutdown: stop the loop, close
the pool.

`uvicorn app.main:app` serves on SERVICE_PORT.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import load_signal
from app.admission import Admission
from app.health import make_health_router
from app.settings import SETTINGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app.main")

_ROUTES = {"T1": "photos"}


def _select_router():
    task = SETTINGS.task
    mod_name = _ROUTES.get(task)
    if mod_name is None:
        raise ValueError(f"unknown TASK {task!r} (expected one of {sorted(_ROUTES)})")
    from importlib import import_module

    return import_module(f"app.routes.{mod_name}").make_router()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db import migrate, open_pool

    pool = await open_pool(os.environ["DB_DSN"])
    await migrate(pool, SETTINGS.task)

    admission = Admission(SETTINGS.admission_concurrency, SETTINGS.admission_wait_s)
    metrics: dict = {"pool_pressure": 0.0, "inflight": 0, "rps": 0.0}

    app.state.task = SETTINGS.task
    app.state.pool = pool
    app.state.admission = admission
    app.state.metrics = metrics

    loop_task = asyncio.create_task(
        load_signal.run_loop(pool, admission, SETTINGS.load_sample_interval_s, latest=metrics)
    )
    log.info("service ready: task=%s port=%d", SETTINGS.task, SETTINGS.port)
    try:
        yield
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await pool.close()


app = FastAPI(title="elastic-cache-service", version="1.0.0", lifespan=lifespan)
app.include_router(make_health_router())
app.include_router(_select_router())
