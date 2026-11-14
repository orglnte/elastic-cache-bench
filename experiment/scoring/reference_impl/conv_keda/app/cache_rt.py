"""Read-through cache path (reference solution).

Admission is entered FIRST, then the cache is consulted: a request that
queued during the warm-up serves from the now-warm cache when admitted
instead of being committed to the store path. Every cache op is time-bounded
so a dying backend can never hang a read — it falls back to the store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

log = logging.getLogger("app.cache_rt")

_CACHE_OP_TIMEOUT_S = float(os.environ.get("CACHE_OP_TIMEOUT_S", "1.0"))


async def cached_read(
    provisioner,
    key: str,
    ttl_s: int,
    loader: Callable[[], Awaitable[object]],
    admission,
) -> tuple[object, str]:
    """Return (value, source), source in {'cache','db'}. `loader` runs the
    store query on a miss, inside the held admission slot."""
    async with admission.enter():
        client = provisioner.cache
        if client is not None:
            try:
                raw = await asyncio.wait_for(client.get(key), _CACHE_OP_TIMEOUT_S)
                if raw is not None:
                    return json.loads(raw.decode("utf-8")), "cache"
            except Exception as e:
                log.warning("cache get failed/timed out for %s (%s); falling back", key, e)
                client = None

        value = await loader()

        if client is not None:
            try:
                await asyncio.wait_for(
                    client.set(key, json.dumps(value).encode("utf-8"), ex=ttl_s),
                    _CACHE_OP_TIMEOUT_S,
                )
            except Exception as e:
                log.warning("cache set failed/timed out for %s (%s)", key, e)
    return value, "db"
