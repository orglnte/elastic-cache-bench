"""The verify host probe benches the capped store on the host the probe is
given, on a store of its own that it always removes."""
from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.cell.verify_host import VerifyHost
from experiment.verifier import probe, rig


class TestStoreBench(unittest.TestCase):
    def run_bench(self, up):
        calls = []
        with mock.patch.object(rig, "db_up", return_value=up) as db_up, \
             mock.patch.object(rig, "db_bench_qps", return_value=61) as bench, \
             mock.patch.object(rig, "_run", side_effect=lambda argv, **kw: calls.append(argv) or (0, "", "")):
            qps = rig.store_bench({"DB_CPUS": "1"}, "probe-7")
        return qps, db_up, bench, calls

    def test_the_store_and_its_network_are_named_for_the_tag(self):
        qps, db_up, bench, _ = self.run_bench((True, {"DB_DSN": "x"}, ""))
        env = db_up.call_args.args[0]
        self.assertEqual((env["DB_CONTAINER"], env["DB_NET"]), ("fae-store-probe-7", "fae-net-probe-7"))
        self.assertEqual(env["DB_CPUS"], "1")
        self.assertEqual(qps, 61)

    def test_both_are_removed_after_a_bench(self):
        _, _, _, calls = self.run_bench((True, {}, ""))
        self.assertIn(["docker", "rm", "-f", "fae-store-probe-7"], calls)
        self.assertIn(["docker", "network", "rm", "fae-net-probe-7"], calls)

    def test_a_store_that_never_came_up_is_no_bench_and_still_removed(self):
        qps, _, bench, calls = self.run_bench((False, {}, "fixed store never became ready"))
        self.assertIsNone(qps)
        bench.assert_not_called()
        self.assertIn(["docker", "rm", "-f", "fae-store-probe-7"], calls)


class TestStoreBenchProbe(unittest.TestCase):
    CONF = {"DB_CPUS": "1", "DB_MEM": "512m", "DB_IMAGE": "postgres:16.15"}

    def measure(self, host, rc=0, out='{"store_qps": 61}\n', err=""):
        done = SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        with mock.patch.object(subprocess, "run", return_value=done) as run, \
             mock.patch.dict("os.environ", {"CPUSET_MEASURED": "0-2"}):
            try:
                return probe.StoreBenchProbe().measure(host, self.CONF, log=lambda m: None), run
            except RuntimeError as e:
                return e, run

    def test_the_bench_runs_against_the_given_hosts_daemon(self):
        got, run = self.measure(VerifyHost("ssh://operator@verify1"))
        self.assertEqual(got, {"store_qps": 61.0})
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual(env["DOCKER_HOST"], "ssh://operator@verify1")
        self.assertEqual(argv[1:3], [str(probe.RIG), "store-bench"])
        self.assertEqual(json.loads(argv[4]), self.CONF)

    def test_a_remote_host_is_not_pinned_to_this_hosts_cores(self):
        _, run = self.measure(VerifyHost("ssh://operator@verify1"))
        self.assertNotIn("CPUSET_MEASURED", run.call_args.kwargs["env"])

    def test_the_local_daemon_keeps_the_measured_cores(self):
        _, run = self.measure(VerifyHost(""))
        env = run.call_args.kwargs["env"]
        self.assertNotIn("DOCKER_HOST", env)
        self.assertEqual(env["CPUSET_MEASURED"], "0-2")

    def test_a_bench_that_did_not_run_raises(self):
        got, _ = self.measure(VerifyHost("ssh://operator@verify1"), rc=1,
                              out='{"store_qps": null}\n', err="store-bench: fixed store never became ready")
        self.assertIsInstance(got, RuntimeError)
        self.assertIn("fixed store never became ready", str(got))


if __name__ == "__main__":
    unittest.main()
