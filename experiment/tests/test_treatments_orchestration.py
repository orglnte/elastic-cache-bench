"""Substrate + overlay orchestration for pulumi and keda variants."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, infra_for

from experiment.variants import keda
from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess
from fae.cell.substrate import DockerContext, K8sContext
from fae.experiment import variants as tr

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
        t = infra_for("pulumi_sealed_apidocs", make_cell("pulumi_sealed_apidocs", self.tmp))
        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(t.ok())

    def test_the_images_the_host_carries_are_all_it_needs(self):
        t = infra_for("pulumi_sealed_apidocs", make_cell("pulumi_sealed_apidocs", self.tmp))
        with mock.patch.object(infra_base, "ok", return_value=True), \
                mock.patch.object(shutil, "which", return_value=None):
            self.assertTrue(t.ok())


class TestPulumiTeardownAndAccess(TreatmentCase):
    def test_access_teardown_removes_the_sidecar(self):
        t = infra_for("pulumi_access_apidocs", make_cell("pulumi_access_apidocs", self.tmp))
        with mock.patch.object(DockerContext, "down") as down:
            t.cell_teardown()
        down.assert_called_once_with(force=True)

    def test_access_setup_returns_docker_host_agent_access(self):
        t = infra_for("pulumi_access_apidocs", make_cell("pulumi_access_apidocs", self.tmp))
        fake_sub = DockerContext(t.cid, scope="cell", in_verify=False, api_port=24111)
        with mock.patch.object(DockerContext, "ensure", return_value=fake_sub):
            acc = t.cell_setup()
        self.assertIsInstance(acc, AgentAccess)
        self.assertEqual(acc.env["DOCKER_HOST"], "tcp://host.docker.internal:24111")


class TestKedaInfraOk(TreatmentCase):
    def test_docker_unreachable_halts(self):
        t = infra_for("kedap_sealed_apidocs", make_cell("kedap_sealed_apidocs", self.tmp))
        with mock.patch.object(infra_base, "ok", return_value=False):
            self.assertFalse(t.ok())

    def test_both_sealed_and_access_need_no_host_kind(self):
        for vid in ("kedap_sealed_apidocs", "kedap_access_apidocs"):
            t = infra_for(vid, make_cell(vid, self.tmp))
            with mock.patch.object(infra_base, "ok", return_value=True), \
                    mock.patch.object(shutil, "which", return_value=None):
                self.assertTrue(t.ok())


class TestKedaAccessSetupAndTeardown(TreatmentCase):
    def test_happy_path_writes_kube_env_and_returns_access(self):
        cell = make_cell("kedap_access_apidocs", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        fake = SimpleNamespace(
            cluster_name="ecb-ka-1",
            network="kind",
            kubeconfig=cell.ws / "kubeconfig",
            host_kubeconfig=cell.ws / "kubeconfig.host",
        )
        with mock.patch.object(K8sContext, "ensure", return_value=fake), \
                mock.patch.object(keda, "_install_keda_stack", return_value="ecb"):
            acc = t.cell_setup()
        self.assertIn("KIND_CLUSTER=ecb-ka-1", (cell.ws / "kube.env").read_text())
        self.assertEqual(acc.network, "kind")
        self.assertEqual(acc.env["KUBECONFIG"], "/home/node/.kube/config")

    def test_a_provision_failure_halts(self):
        cell = make_cell("kedap_access_apidocs", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        with mock.patch.object(K8sContext, "ensure", side_effect=tr.HookFailure("kind create failed")):
            with self.assertRaises(tr.HookFailure):
                t.cell_setup()

    def test_teardown_deletes_env_files_and_the_cluster(self):
        cell = make_cell("kedap_access_apidocs", self.tmp)
        t = infra_for("kedap_access_apidocs", cell)
        (cell.ws / "kube.env").write_text("x")
        with mock.patch.object(K8sContext, "down") as down:
            t.cell_teardown()
        down.assert_called_once_with(force=True)
        self.assertFalse((cell.ws / "kube.env").exists())


if __name__ == "__main__":
    unittest.main()
