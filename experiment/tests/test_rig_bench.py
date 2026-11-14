"""rig.db_bench_qps distinguishes a bench that ran from one that did not."""
from __future__ import annotations

import unittest
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)
from experiment.verifier import rig

ENV = {"DB_NAME": "d", "DB_CONTAINER": "c", "DB_NET": "n", "DB_IMAGE": "postgres:16.15"}


class TestDbBenchQps(unittest.TestCase):
    def bench(self, rc, out):
        with mock.patch.object(rig, "_db", return_value={"name": "d", "container": "c",
                                                           "net": "n", "image": "i",
                                                           "cpuset": "0-2"}), \
             mock.patch.object(rig, "_run", return_value=(rc, out, "")):
            return rig.db_bench_qps(ENV, 5000)

    def test_tps_line_is_parsed(self):
        self.assertEqual(self.bench(0, "latency average = 1 ms\ntps = 61.7 (without initial)\n"), 61)

    def test_a_failed_bench_is_none_not_zero(self):
        self.assertIsNone(self.bench(1, ""))
        self.assertIsNone(self.bench(125, "docker: Error response from daemon: no such container"))

    def test_no_tps_line_is_none(self):
        self.assertIsNone(self.bench(0, "pgbench: connection refused\n"))


if __name__ == "__main__":
    unittest.main()
