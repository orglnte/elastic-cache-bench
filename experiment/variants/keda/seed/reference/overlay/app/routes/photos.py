"""FIXED SKELETON — do not edit. Task T1 (photoshare) routes.

The hot read path is GET /photos/search — it runs the CPU-heavy ranked query
and is served through the read-through cache. A load spike on it saturates
the pool, pool_pressure climbs, and the cache 0<->1 event fires. POST/GET
metadata paths keep the service functional end to end.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.admission import AdmissionTimeout
from app.cache import cached_read
from app.db import burn_and_rank


def make_router() -> APIRouter:
    router = APIRouter()

    @router.post("/photos", status_code=201)
    async def create_photo(request: Request, body: dict = Body(...)) -> dict:
        owner = str(body.get("owner", "")).strip()
        caption = str(body.get("caption", ""))
        if not owner:
            raise HTTPException(status_code=422, detail="owner is required")
        pid = uuid.uuid4().hex
        st = request.app.state
        try:
            async with st.admission.enter():
                async with st.pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO photos (id, owner, caption) VALUES (%s, %s, %s)",
                        (pid, owner, caption),
                    )
                    await conn.commit()
        except AdmissionTimeout:
            raise HTTPException(status_code=503, detail="overloaded, retry")
        return {"id": pid, "owner": owner, "caption": caption}

    @router.get("/photos/search")
    async def search_photos(request: Request, q: str = Query(default="sunset")) -> dict:
        st = request.app.state

        async def _loader() -> dict:
            # store query only — admission is entered by cached_read (around the
            # cache check), so a warm-up waiter serves from cache, not the store.
            async with st.pool.connection() as conn, conn.cursor() as cur:
                await burn_and_rank(cur, q)
                await cur.execute(
                    "SELECT id, owner, caption FROM photos "
                    "WHERE caption ILIKE %s ORDER BY created_at DESC LIMIT 20",
                    (f"%{q}%",),
                )
                rows = await cur.fetchall()
            return {"query": q, "photos": [{"id": r[0], "owner": r[1], "caption": r[2]} for r in rows]}

        try:
            value, source = await cached_read(st.prov, f"search:{q}", ttl_s=30, loader=_loader, admission=st.admission)
        except AdmissionTimeout:
            raise HTTPException(status_code=503, detail="overloaded, retry")
        return {**value, "source": source}

    @router.get("/photos")
    async def list_photos(request: Request, owner: str = Query(...), limit: int = Query(20, le=100)) -> dict:
        st = request.app.state
        try:
            async with st.admission.enter():
                async with st.pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, owner, caption FROM photos WHERE owner = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (owner, limit),
                    )
                    rows = await cur.fetchall()
        except AdmissionTimeout:
            raise HTTPException(status_code=503, detail="overloaded, retry")
        return {"owner": owner, "photos": [{"id": r[0], "owner": r[1], "caption": r[2]} for r in rows]}

    @router.get("/photos/{photo_id}")
    async def get_photo(request: Request, photo_id: str) -> dict:
        st = request.app.state
        async with st.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT id, owner, caption FROM photos WHERE id = %s", (photo_id,))
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="photo not found")
        return {"id": row[0], "owner": row[1], "caption": row[2]}

    return router
