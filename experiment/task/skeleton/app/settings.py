"""Process-level settings read from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Which task's route set to serve (T1=photoshare). Set by the bring-up.
    task: str = os.environ.get("TASK", "T1").upper()

    # The FIXED HTTP port the entrypoint contract mandates (deploy.sh must
    # make GET /health reachable here). Overridable only for local dev.
    port: int = int(os.environ.get("SERVICE_PORT", "8080"))

    # Admission concurrency: bounds in-flight DB work so a load spike
    # queues at the semaphore (traffic shaping) instead of stampeding the
    # pool. Skeleton-provided so traffic shaping is identical across
    # variants and does not confound the provisioning-slice signal.
    admission_concurrency: int = int(
        os.environ.get("ADMISSION_CONCURRENCY", str(int(os.environ.get("POOL_MAX", "16")) * 5))
    )

    # How long a request will wait for an admission slot before the
    # skeleton sheds it with 503 (bounded queue, not unbounded pile-up).
    admission_wait_s: float = float(os.environ.get("ADMISSION_WAIT_S", "20"))

    # Cadence at which the load-signal loop samples pool pressure
    # (bounded cadence, not per-request).
    load_sample_interval_s: float = float(os.environ.get("LOAD_SAMPLE_INTERVAL_S", "1.0"))


SETTINGS = Settings()
