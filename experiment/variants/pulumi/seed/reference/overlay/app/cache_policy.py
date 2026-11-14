"""Cache-provisioning decision logic — a pure control policy, no I/O.

This is deliberately separated from the Pulumi calls and the Redis client so
the decision that matters — *when* to mount and *when* to release the cache —
can be unit-tested with no daemon, no store, and no network (see
``docs/pulumi.md`` "Validate offline before submitting").

The control theory (from ``docs/pulumi.md`` "drive up/destroy from a noisy
signal"):

1. Act on a condition that *holds* for several consecutive samples, in BOTH
   directions — not on a single instantaneous sample. A short blip must not
   trip the mount; a momentary dip must not trip the release.
2. Asymmetric directions (hysteresis): the acquire signal and the release
   signal are different signals, so the resource cannot oscillate at a
   boundary.
3. Expect the signal to move *because* of the action. Once the cache absorbs
   the hot reads, ``pool_pressure`` — the signal that justified the mount —
   collapses. Releasing on low pressure would tear down the very resource
   doing the job. So release is judged on the *demand the cache is currently
   serving* (hot-path arrival rate), NOT on pool pressure.

This is the same golden-signal law the KEDA references author for
this task — mount on SATURATION AND LATENCY sustained, release on TRAFFIC
below the store's SLO-safe knee sustained — assembled here as its own
in-process Python control loop instead of an SDK scaler composition or an
external autoscaler's trigger stack. Same law, third authoring locus.

Mount signal:   ``pool_pressure`` AND ``p99_ms`` both sustained at/above a
                threshold (saturation AND latency breaching) while demand is
                above the release knee — a backlog still draining under
                baseline traffic saturates the pool and breaches latency
                exactly like a spike, and is the one thing the cache must
                not be re-mounted for.
Release signal: hot-path arrival rate sustained at/below the store's
                SLO-safe knee (the spike that needed the cache is over).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Decision(Enum):
    NONE = "none"
    MOUNT = "mount"
    RELEASE = "release"


def _envf(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _envi(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class PolicyConfig:
    # Mount when pool_pressure >= mount_pressure AND p99_ms >= mount_latency_ms
    # AND hot_rps > release_rps for mount_samples consecutive samples. The
    # first AND is why a cache-absorbed blip (pressure spikes, latency does
    # not) never mounts on saturation alone; the demand term is why a store
    # still draining a spike's backlog under baseline traffic does not either
    # — mounting there would be released by the very next release check.
    mount_pressure: float = _envf("CACHE_MOUNT_PRESSURE", 0.7)
    mount_latency_ms: float = _envf("CACHE_MOUNT_LATENCY_MS", 150.0)
    mount_samples: int = _envi("CACHE_MOUNT_SAMPLES", 5)

    # Release when the hot-path arrival rate falls to <= release_rps for
    # release_samples consecutive samples — the store's SLO-safe knee, fixed
    # rather than learned: rps is the one golden signal the cache does not
    # mask (a working cache always drops pool_pressure and latency, whatever
    # traffic is doing), so it needs no adaptation to stay honest.
    release_rps: float = _envf("CACHE_RELEASE_RPS", 30.0)
    release_samples: int = _envi("CACHE_RELEASE_SAMPLES", 3)


class CachePolicy:
    """Stateful streak tracker. Feed it one (pool_pressure, p99_ms, hot_rps)
    sample per control tick via :meth:`observe`; it returns a
    :class:`Decision`.

    ``mounted`` is the policy's intended state. The controller flips it back
    (via :meth:`revert`) if an actual mount/release operation fails, so the
    next sustained condition retries.
    """

    def __init__(self, cfg: PolicyConfig | None = None) -> None:
        self.cfg = cfg or PolicyConfig()
        self.mounted = False
        self.high_streak = 0
        self.low_streak = 0

    def observe(self, pool_pressure: float, p99_ms: float, hot_rps: float) -> Decision:
        cfg = self.cfg

        high = (pool_pressure >= cfg.mount_pressure and p99_ms >= cfg.mount_latency_ms
                and hot_rps > cfg.release_rps)
        self.high_streak = self.high_streak + 1 if high else 0
        low = hot_rps <= cfg.release_rps
        self.low_streak = self.low_streak + 1 if low else 0

        if not self.mounted:
            if self.high_streak >= cfg.mount_samples:
                self.mounted = True
                self.high_streak = 0
                self.low_streak = 0
                return Decision.MOUNT
        else:
            if self.low_streak >= cfg.release_samples:
                self.mounted = False
                self.high_streak = 0
                self.low_streak = 0
                return Decision.RELEASE
        return Decision.NONE

    def revert(self, intended_mounted: bool) -> None:
        """Called when the real mount/release operation failed: put the
        intended state back so the condition can retry on the next samples."""
        self.mounted = not intended_mounted
        self.high_streak = 0
        self.low_streak = 0
