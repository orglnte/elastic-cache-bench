"""Verify.run() itself — the deploy/assert/load/judge/teardown sequencing —
had zero coverage: every other verify.py test drives one method (deploy(),
e2e(), ...) directly. This drives run() for real with Verify's OWN methods
stubbed, like a mocked smoke test: the orchestration and the finally-block
cleanup discipline are real; the infra each stubbed method would reach
(docker, k6, urllib) is not. `cli.py experiment smoke` proves the infra.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT
from experiment import verifier as verify

ROOT = Path(ROOT)


class VerifyRunCase(unittest.TestCase):
    """Every method run() calls is stubbed to its green-path value; each test
    below overrides only the one thing it's testing. Cleanup methods
    (teardown/_gzip) are real Mocks so every test can assert they ran exactly
    once — run()'s `finally` block is the whole point of this file. The rig
    lock, the fingerprint pin, metrics.json and the archive are the engine's
    (fae/cell/cell.py), outside this process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        art = d / "artifacts"
        out = d / "ws"
        art.mkdir()
        out.mkdir()
        self.v = verify.Verify(art, out, task="T1", variant="pulumi_sealed_apidocs",
                               cid="cell-x", root=ROOT, config={})
        v = self.v
        v.tech = mock.Mock(return_value="pulumi")
        v.adopt_cell_env = mock.Mock()
        v.db_up = mock.Mock(return_value=(True, ""))
        v.bench_ceiling = mock.Mock(return_value=100)
        v.deploy = mock.Mock(return_value="ok")
        v.readiness = mock.Mock(return_value=(10, 10))
        v.keda_warmup = mock.Mock()
        v.e2e = mock.Mock(side_effect=self._green_e2e)
        v.http = mock.Mock(return_value=(200, '{"cache_mounted": false}'))
        v.run_load = mock.Mock(side_effect=self._green_run_load)
        v.teardown = mock.Mock()
        v._gzip = mock.Mock()
        self.contract_patch = mock.patch.object(
            verify.contract, "for_variant",
            return_value=mock.Mock(after_deploy=mock.Mock(return_value=[])))
        self.contract_patch.start()
        self.addCleanup(self.contract_patch.stop)
        self.clean_patch = mock.patch.object(verify.rig, "clean_start_wait",
                                             return_value=True)
        self.clean_patch.start()
        self.addCleanup(self.clean_patch.stop)
        self.caps_patch = mock.patch.object(verify.rig, "db_assert_caps",
                                            return_value=None)
        self.caps_patch.start()
        self.addCleanup(self.caps_patch.stop)

    def _green_e2e(self):
        self.v.result.e2e_green = True
        return True

    def _green_run_load(self, *a, **kw):
        self.v.result.scaling_ok = True

    def cleanup_calls(self):
        v = self.v
        return (v.teardown.call_count, v._gzip.call_count)


class TestTheHappyPath(VerifyRunCase):
    def test_a_clean_run_is_green_and_cleans_up_exactly_once(self):
        r = self.v.run()
        self.assertTrue(r.green)
        self.assertEqual(r.stage_failed, "")
        self.assertEqual(self.cleanup_calls(), (1, 1))
        self.v.teardown.assert_called_once_with("pulumi")


class TestEveryOtherFailureStillCleansUp(VerifyRunCase):
    """The finally block runs no matter which stage failed — that's what makes a failed attempt still refund its
    infra."""

    def run_and_check(self, expected_stage):
        r = self.v.run()
        self.assertEqual(r.stage_failed, expected_stage)
        self.assertFalse(r.green)
        self.assertEqual(self.cleanup_calls(), (1, 1))
        return r

    def test_db_up_failure_is_store(self):
        self.v.db_up.return_value = (False, "boom")
        self.run_and_check("store")

    def test_clean_start_wait_failure_is_nostart(self):
        self.clean_patch.stop()
        with mock.patch.object(verify.rig, "clean_start_wait", return_value=False):
            self.run_and_check("nostart")
        self.clean_patch.start()

    def test_deploy_missing_bringup_is_deploy(self):
        self.v.deploy.return_value = "missing"
        self.run_and_check("deploy")

    def test_deploy_rejected_is_deploy(self):
        self.v.deploy.return_value = "rejected"
        self.run_and_check("deploy")

    def test_deploy_nostart_is_nostart(self):
        self.v.deploy.return_value = "nostart"
        self.run_and_check("nostart")

    def test_deploy_contract_reports_the_pre_deploy_violations(self):
        self.v.deploy.return_value = "contract"
        self.v.pre_deploy = ["service X missing"]
        r = self.run_and_check("contract")
        self.assertEqual(r.contract, ["service X missing"])

    def test_after_deploy_contract_violation_is_contract(self):
        self.contract_patch.stop()
        with mock.patch.object(
                verify.contract, "for_variant",
                return_value=mock.Mock(
                    after_deploy=mock.Mock(return_value=["port mismatch"]))):
            r = self.run_and_check("contract")
        self.assertEqual(r.contract, ["port mismatch"])
        self.contract_patch.start()

    def test_readiness_never_holding_is_nostart(self):
        self.v.readiness.return_value = (2, 10)
        self.run_and_check("nostart")

    def test_cache_mounted_at_idle_is_precheck(self):
        self.v.http.return_value = (200, '{"cache_mounted": true}')
        r = self.run_and_check("precheck")
        self.assertFalse(r.e2e_green)

    def test_a_capped_store_not_enforced_is_precheck(self):
        self.caps_patch.stop()
        with mock.patch.object(verify.rig, "db_assert_caps", return_value="uncapped"):
            r = self.run_and_check("precheck")
        self.assertFalse(r.e2e_green)
        self.caps_patch.start()


class TestE2EFailureReturnsWithoutOverridingStage(VerifyRunCase):
    def test_a_failed_e2e_still_cleans_up_but_sets_no_stage(self):
        self.v.e2e = mock.Mock(return_value=False)
        r = self.v.run()
        self.assertFalse(r.green)
        self.assertEqual(r.stage_failed, "")
        self.assertEqual(self.cleanup_calls(), (1, 1))


class TestHostOverloadedRetriesTheBench(VerifyRunCase):
    def test_a_low_ceiling_re_benches_until_it_clears_the_floor(self):
        self.v.bench_ceiling.side_effect = [10, 10, 100]
        self.v.cfg["CALIBRATION_MIN_QPS"] = 40
        with mock.patch.object(verify.time, "sleep") as sleep:
            r = self.v.run()
        self.assertEqual(self.v.bench_ceiling.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(r.green)

    def test_a_bench_that_did_not_run_is_a_store_failure_not_host_load(self):
        self.v.bench_ceiling.return_value = None
        with mock.patch.object(verify.time, "sleep") as sleep:
            r = self.v.run()
        self.assertEqual(r.stage_failed, "store")
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(self.v.bench_ceiling.call_count, 1)

    def test_a_ceiling_that_never_clears_the_floor_stops_re_benching(self):
        self.v.bench_ceiling.return_value = 10
        self.v.cfg["CALIBRATION_MIN_QPS"] = 40
        with mock.patch.object(verify.time, "sleep") as sleep:
            r = self.v.run()
        self.assertEqual(r.stage_failed, "store")
        self.assertEqual(sleep.call_count, verify.Verify.MAX_REBENCHES)
        self.assertEqual(self.v.bench_ceiling.call_count, verify.Verify.MAX_REBENCHES + 1)


if __name__ == "__main__":
    unittest.main()
