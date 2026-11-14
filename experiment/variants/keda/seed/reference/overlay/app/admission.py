"""FIXED SKELETON — do not edit.

Admission gate + in-flight/RPS accounting. A bounded semaphore upstream of
every DB access: under a spike, excess requests QUEUE here (bounded) rather
than stampeding the pool or being blanket-rejected. If a request waits
longer than the configured budget it is shed with 503 (bounded queue, not
unbounded pile-up) — this is the skeleton's traffic-shaping-across-the-
transition behavior, identical for both variants so it does not confound
the provisioning-slice measurement.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class Admission:
    def __init__(self, concurrency: int, wait_s: float) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._wait_s = wait_s
        self._inflight = 0
        # timestamps of recently completed admissions, for a rough RPS.
        self._recent: deque[float] = deque(maxlen=2048)
        # (completion_ts, served_latency_ms) for a rolling p99 — the SLO signal.
        self._lat: deque[tuple[float, float]] = deque(maxlen=4096)

    @property
    def inflight(self) -> int:
        return self._inflight

    def rps(self, window_s: float = 5.0) -> float:
        now = time.monotonic()
        n = sum(1 for t in self._recent if now - t <= window_s)
        return n / window_s

    def p99(self, window_s: float = 5.0) -> float:
        """Served-latency p99 (ms) over the window — the SLO golden signal.
        Timed from admission attempt to completion, so it INCLUDES the queue
        wait: the real user-facing latency, not just DB time."""
        now = time.monotonic()
        vals = sorted(d for t, d in self._lat if now - t <= window_s)
        if not vals:
            return 0.0
        return vals[max(0, min(len(vals) - 1, int(0.99 * len(vals))))]

    def enter(self) -> "_AdmissionCtx":
        return _AdmissionCtx(self)


class AdmissionTimeout(Exception):
    """Raised when a request could not get an admission slot in time.
    Routes translate this to HTTP 503."""


class _AdmissionCtx:
    def __init__(self, gate: Admission) -> None:
        self._gate = gate

    async def __aenter__(self) -> None:
        self._t_enter = time.monotonic()  # before the semaphore wait -> latency includes queue
        try:
            await asyncio.wait_for(self._gate._sem.acquire(), timeout=self._gate._wait_s)
        except asyncio.TimeoutError as e:
            raise AdmissionTimeout() from e
        self._gate._inflight += 1

    async def __aexit__(self, *exc) -> None:
        self._gate._inflight -= 1
        now = time.monotonic()
        self._gate._recent.append(now)
        self._gate._lat.append((now, (now - self._t_enter) * 1000.0))
        self._gate._sem.release()
