"""The references author ONE cache law. Every mount predicate carries
the same three terms — saturation, latency, and demand above the release
knee — so a spike's backlog draining under tail traffic never re-mounts on
any arm."""
import unittest
from pathlib import Path

from _ctx import ROOT

REF = Path(ROOT) / "experiment" / "variants"


class TestEveryReferenceMountsOnlyAboveTheKnee(unittest.TestCase):

    def test_pulumi(self):
        src = (REF / "pulumi" / "seed" / "reference" / "overlay" / "app" / "cache_policy.py").read_text()
        self.assertIn("and hot_rps > cfg.release_rps", src)

    def test_keda(self):
        src = (REF / "keda" / "seed" / "reference" / "overlay" / "k8s" / "prometheus.yaml").read_text()
        acquire = src[src.index("- record: cache:acquire"):src.index("- record: cache:hold")]
        self.assertIn("(min_over_time(app_rps[5s]) > bool 30)", acquire)


if __name__ == "__main__":
    unittest.main()
