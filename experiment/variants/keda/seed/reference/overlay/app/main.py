"""FIXED SKELETON — do not edit.

Application wiring. The lifespan is the whole integration point:

  1. build the provisioning slice (app/provisioning_impl.py — the ONE file
     the agent authors);
  2. start it -> Backends (startup resources provisioned);
  3. open the Postgres pool from Backends.db_dsn and migrate;
  4. run the load-signal loop (samples pool pressure, feeds the slice,
     updates GET /metrics);
  5. on shutdown, stop the load loop, close the pool, stop the slice.

`uvicorn app.main:app` serves this on SERVICE_PORT. Identical for both
variants — only build_provisioning() differs.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import load_signal
from app.admission import Admission
from app.health import make_health_router
from app.provisioning_impl import build_provisioning
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

    prov = build_provisioning(SETTINGS)
    backends = await prov.start()
    pool = await open_pool(backends.db_dsn)
    await migrate(pool, SETTINGS.task)

    admission = Admission(SETTINGS.admission_concurrency, SETTINGS.admission_wait_s)
    metrics: dict = {"pool_pressure": 0.0, "inflight": 0, "rps": 0.0}

    app.state.task = SETTINGS.task
    app.state.prov = prov
    app.state.pool = pool
    app.state.admission = admission
    app.state.metrics = metrics

    loop_task = asyncio.create_task(
        load_signal.run_loop(pool, admission, prov, SETTINGS.load_sample_interval_s, latest=metrics)
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
        await prov.stop()


app = FastAPI(title="elastic-cache-service", version="1.0.0", lifespan=lifespan)
app.include_router(make_health_router())
app.include_router(_select_router())
