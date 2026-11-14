"""experiment/variants/: the infra classes' own cell_setup()/cell_teardown()/
ok() bodies — the sequencing each orchestrates over DindSidecar/
KindCluster. test_treatments.py already covers those helper
classes' own mechanisms (ports, env files, sidecar lifecycle) in isolation; this drives the arm-level orchestration for real,
with those helper classes mocked at the boundary — the same "mocked e2e"
shape as test_verify_run_orchestration.py for Verify.run().
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, infra_for

from fae.experiment import variants as tr
from fae.cell.infra import base as infra_base
from experiment import variants as sj
from experiment.verifier import rig as exp_rig

ROOT = Path(ROOT)


def make_cell(variant, tmp, root=None):
    ws = tmp / "cid1"
    ws.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(cid="cid1", variant=variant,
                           root=root or ROOT, ws=ws,
                           conf=SimpleNamespace(get=lambda k, d=None: d))


class TreatmentCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)










class TestPulumiInfraOk(TreatmentCase):
    def test_docker_unreachable_halts(self):
        t = infra_for("pulumi_sealed_apidocs", make_cell("pulumi_sealed", self.tmp))
        with mock.patch.object(infra_base, "_ok", return_value=False):
            self.assertFalse(t.ok())

    def test_the_images_the_host_carries_are_all_it_needs(self):
        t = infra_for("pulumi_sealed_apidocs", make_cell("pulumi_sealed", self.tmp))
        with mock.patch.object(infra_base, "_ok", return_value=True), \
                mock.patch.object(shutil, "which", return_value=None):
            self.assertTrue(t.ok())


class TestPulumiTeardownAndAccess(TreatmentCase):
    def test_teardown_removes_the_sidecar(self):
        t = infra_for("pulumi_sealed_apidocs", make_cell("pulumi_sealed", self.tmp))
        fake_sidecar = mock.Mock()
        t.sidecar = mock.Mock(return_value=fake_sidecar)
        t.cell_teardown()
        fake_sidecar.remove.assert_called_once()

    def test_access_setup_adds_the_docker_host_kube_mount(self):
        t = infra_for("pulumi_access_apidocs", make_cell("pulumi_access", self.tmp))
        fake_sidecar = mock.Mock()
        t.sidecar = mock.Mock(return_value=fake_sidecar)
        env = t.cell_setup()
        fake_sidecar.ensure.assert_called_once()
        self.assertIn("DOCKER_HOST=tcp://host.docker.internal:", env["KUBE_MOUNT"])


class TestKedaInfraOk(TreatmentCase):
    def test_docker_unreachable_halts(self):
        t = infra_for("kedap_sealed_apidocs", make_cell("keda_sealed", self.tmp))
        with mock.patch.object(infra_base, "_ok", return_value=False):
            self.assertFalse(t.ok())

    def test_the_sealed_arm_needs_no_host_tool(self):
        # kind and kubectl are the verify image's
        t = infra_for("kedap_sealed_apidocs", make_cell("keda_sealed", self.tmp))
        with mock.patch.object(infra_base, "_ok", return_value=True), \
                mock.patch.object(shutil, "which", return_value=None):
            self.assertTrue(t.ok())

    def test_the_access_arms_sandbox_needs_no_host_tool_either(self):
        # the sandbox is provisioned through the keda image (Variant.tool)
        t = infra_for("kedap_access_apidocs", make_cell("keda_access", self.tmp))
        with mock.patch.object(infra_base, "_ok", return_value=True), \
                mock.patch.object(shutil, "which", return_value=None):
            self.assertTrue(t.ok())


class TestKedaAccessSetup(TreatmentCase):
    def _fake_cluster(self, name="ecb-ka-1", net="kind"):
        c = mock.Mock()
        c.provision.return_value = name
        c.network.return_value = net
        return c

    def test_happy_path_writes_kube_env_and_returns_mounts(self):
        cell = make_cell("keda_access", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        fake = self._fake_cluster()
        with mock.patch.object(sj.keda, "KindCluster", return_value=fake):
            env = t.cell_setup()
        self.assertIn("KIND_CLUSTER=ecb-ka-1", (cell.ws / "kube.env").read_text())
        self.assertEqual(env["DOCKER_NET"], "--network kind")
        self.assertIn("KUBECONFIG=/home/node/.kube/config", env["KUBE_MOUNT"])

    def test_a_provision_failure_halts(self):
        cell = make_cell("keda_access", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        fake = mock.Mock()
        fake.provision.side_effect = tr.HookFailure("kind create failed")
        with mock.patch.object(sj.keda, "KindCluster", return_value=fake):
            with self.assertRaises(tr.HookFailure):
                t.cell_setup()


class TestKedaAccessTeardown(TreatmentCase):
    def test_deletes_env_files_and_the_cluster(self):
        cell = make_cell("keda_access", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        (cell.ws / "kubeconfig").write_text("x")
        (cell.ws / "kube.env").write_text("x")
        fake = mock.Mock()
        with mock.patch.object(sj.keda, "KindCluster", return_value=fake):
            t.cell_teardown()
        fake.delete.assert_called_once()
        self.assertFalse((cell.ws / "kubeconfig").exists())
        self.assertFalse((cell.ws / "kube.env").exists())


if __name__ == "__main__":
    unittest.main()
