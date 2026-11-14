"""The pulumi reference solution's own release policy: mount on saturation
AND latency sustained, release on a fixed rps knee sustained — the same
golden-signal law the keda references author, no learned baseline to
poison (the class of bug this file used to pin against is now structurally
absent, not just guarded)."""
import importlib.util
import sys
import unittest
from pathlib import Path

from _ctx import ROOT

_path = (Path(ROOT) / "experiment" / "variants" / "pulumi" / "seed" / "reference" /
         "overlay" / "app" / "cache_policy.py")
_spec = importlib.util.spec_from_file_location("_t_cache_policy", _path)
cp = importlib.util.module_from_spec(_spec)
sys.modules["_t_cache_policy"] = cp
_spec.loader.exec_module(cp)


class TestTheMountSignalNeedsBothSaturationAndLatency(unittest.TestCase):

    def test_pressure_alone_never_mounts(self):
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=1.0, p99_ms=0.0, hot_rps=0.0)
                     for _ in range(10)]
        self.assertNotIn(cp.Decision.MOUNT, decisions)

    def test_latency_alone_never_mounts(self):
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=0.0, p99_ms=1000.0, hot_rps=0.0)
                     for _ in range(10)]
        self.assertNotIn(cp.Decision.MOUNT, decisions)

    def test_both_sustained_mounts(self):
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=1.0, p99_ms=1000.0, hot_rps=120.0)
                     for _ in range(p.cfg.mount_samples)]
        self.assertEqual(decisions[-1], cp.Decision.MOUNT)

    def test_a_short_blip_does_not_mount(self):
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=1.0, p99_ms=1000.0, hot_rps=120.0)
                     for _ in range(p.cfg.mount_samples - 1)]
        self.assertNotIn(cp.Decision.MOUNT, decisions)

    def test_a_draining_backlog_under_baseline_traffic_never_mounts(self):
        """Saturation and latency both breaching while demand sits at the
        tail level is a backlog draining, not a spike: mounting there would
        be released by the next release check (the 2026-08-28 SSB third
        episode)."""
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=1.0, p99_ms=15000.0, hot_rps=18.0)
                     for _ in range(10)]
        self.assertNotIn(cp.Decision.MOUNT, decisions)

    def test_the_same_signals_with_spike_demand_do_mount(self):
        p = cp.CachePolicy()
        decisions = [p.observe(pool_pressure=1.0, p99_ms=15000.0, hot_rps=120.0)
                     for _ in range(p.cfg.mount_samples)]
        self.assertEqual(decisions[-1], cp.Decision.MOUNT)


class TestTheReleaseSignalIsAFixedKneeNoLearning(unittest.TestCase):

    def _mounted(self):
        p = cp.CachePolicy()
        for _ in range(p.cfg.mount_samples):
            p.observe(1.0, 1000.0, 120.0)
        self.assertTrue(p.mounted)
        return p

    def test_traffic_before_deploy_never_influences_the_release_knee(self):
        """The pool this used to be a regression for: ticks with no traffic at
        all (deploy, before the first arrival) used to decay a learned
        baseline. There is nothing left to poison — the knee is fixed."""
        p = cp.CachePolicy()
        for _ in range(20):
            p.observe(0.0, 0.0, 0.0)          # pre-load idle
        self.assertEqual(p.cfg.release_rps, cp.PolicyConfig().release_rps)

    def test_release_fires_on_demand_at_or_below_the_knee_sustained(self):
        p = self._mounted()
        decisions = [p.observe(0.0, 0.0, p.cfg.release_rps)
                     for _ in range(p.cfg.release_samples)]
        self.assertEqual(decisions[-1], cp.Decision.RELEASE)

    def test_demand_above_the_knee_never_releases(self):
        p = self._mounted()
        decisions = [p.observe(0.0, 0.0, p.cfg.release_rps + 1.0)
                     for _ in range(20)]
        self.assertNotIn(cp.Decision.RELEASE, decisions)

    def test_release_fires_inside_a_shaped_tail_still_carrying_traffic(self):
        """Regression for the 2026-08-28 near-miss: a fixed knee releases as
        soon as demand sustains at/below it, even while the tail is still
        trafficked — it does not wait for traffic to go fully silent."""
        p = self._mounted()
        decisions = [p.observe(0.0, 0.0, 12.0) for _ in range(p.cfg.release_samples)]
        self.assertIn(cp.Decision.RELEASE, decisions)


if __name__ == "__main__":
    unittest.main()
