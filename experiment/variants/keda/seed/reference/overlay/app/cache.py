"""FIXED SKELETON — do not edit.

The read-through cache path. Every hot read routes through `cached_read`,
which asks the provisioning slice for the CURRENT cache client. Three cases,
all handled here so the routes stay identical across variants:

  * client present, key hit  -> serve from cache
  * client present, key miss -> run the loader, populate cache, serve
  * client is None           -> run the loader, serve (transactional-store
                                fallback: before first acquire, after
                                release, or while a scale-up has not landed)

Because the None case is a first-class path, "a read must fall back to
Postgres after the cache is released" and "never assume the requested
capacity landed" hold structurally, for BOTH variants, without the routes
knowing which provisioning surface is behind the seam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

from app.contracts import Provisioning

log = logging.getLogger("app.cache")

# Hard time bound on any single cache operation. A cache backend that is
# unmounting/terminating (e.g. KEDA scaling the Redis pod to 0 out-of-band) can
# accept the TCP connection but never answer, and the client's own socket
# timeout does not always fire on a half-open pod — the read would then hang to
# the request timeout. Bounding every op here guarantees a fast fall-back to the
# store no matter what the cache client does internally. Identical for both
# variants (an in-code policy releases on its own and never hits this; an
# external autoscaler does, when the external autoscaler removes Redis under load).
_CACHE_OP_TIMEOUT_S = float(os.environ.get("CACHE_OP_TIMEOUT_S", "1.0"))


async def cached_read(
    prov: Provisioning,
    key: str,
    ttl_s: int,
    loader: Callable[[], Awaitable[object]],
    admission,
) -> tuple[object, str]:
    """Return (value, source) where source is 'cache' | 'db'. `value` must be
    JSON-serialisable. `loader` hits the transactional store on a miss.

    ORDERING (the whole point): enter the admission gate FIRST, then check the
    cache. A request that queued at the gate during the cache warm-up therefore
    serves from the (now warm) cache when it is admitted, instead of being
    committed to the store path — so the pre-cache backlog drains at cache speed
    once the cache mounts. `loader` (the store query) runs only on a genuine
    miss, inside the held slot, and must NOT re-enter admission. Raises
    AdmissionTimeout if no slot is granted within the budget (routes -> 503)."""
    async with admission.enter():
        client = prov.cache()
        if client is not None:
            try:
                raw = await asyncio.wait_for(client.get(key), _CACHE_OP_TIMEOUT_S)
                if raw is not None:
                    return json.loads(raw.decode("utf-8")), "cache"
            except Exception as e:  # a mounting/unmounting cache must never 500 OR hang a read
                log.warning("cache get failed/timed out for %s (%s); falling back to db", key, e)
                client = None

        value = await loader()

        if client is not None:
            try:
                await asyncio.wait_for(
                    client.set(key, json.dumps(value).encode("utf-8"), ttl_s),
                    _CACHE_OP_TIMEOUT_S,
                )
            except Exception as e:
                log.warning("cache set failed/timed out for %s (%s)", key, e)
    return value, "db"
