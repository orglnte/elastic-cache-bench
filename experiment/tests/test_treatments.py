"""Variant substrates and domain overlays (`experiment/variants/`): registry,
AgentAccess shaping, split lifecycle skipping unused cell sandboxes, KEDA
manifest image parsing and metrics-server installation."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ENGINE_TREE, ROOT, infra_for, tech_of

from experiment.variants import keda
from fae.cell.infra import base as infra_base
from fae.cell.infra.base import AgentAccess
from fae.cell.substrate import DockerContext, DockerSubstrate, K8sContext, K8sSubstrate, cluster_name_for
from fae.experiment import variants as tr


def fake_cell(cid="opus_high_pulumi_sealed_apidocs_T1_r1", variant="pulumi_sealed_apidocs",
              root=None, ws=None):
    return SimpleNamespace(cid=cid, variant=variant,
                           root=root or ROOT, ws=ws or Path("/nonexistent") / cid,
                           conf=SimpleNamespace(get=lambda k, d=None: d))


class TestTheRegistry(unittest.TestCase):

    def test_every_variant_has_the_substrate_of_its_tech(self):
        self.assertEqual(len(tr.registry()), 6)
        for vid, cls in tr.registry().items():
            self.assertEqual(cls.ID, vid)
            want = "keda" if vid.startswith("keda") else vid.split("_")[0]
            self.assertEqual(tech_of(cls), want)
            self.assertEqual(cls.FACTORS["tech"], want)
            self.assertIsNotNone(cls.SUBSTRATE)
            if want == "pulumi":
                self.assertTrue(issubclass(cls.INFRA, DockerSubstrate))
            else:
                self.assertTrue(issubclass(cls.INFRA, K8sSubstrate))

    def test_the_noop_seam(self):
        with mock.patch.dict(os.environ, {tr.NOOP_ENV: "1"}):
            t = tr.for_cell(fake_cell())
        self.assertIsInstance(t, tr.NoopInfra)
        self.assertEqual(t.cell_setup(), {})
        self.assertTrue(t.ok())
        t.cell_teardown()

    def test_an_unknown_variant_provisions_nothing(self):
        self.assertIsInstance(tr.for_cell(fake_cell(variant="mystery")),
                              tr.NoopInfra)


class TestPortsAndNames(unittest.TestCase):
    def test_cksum_is_posix_cksum(self):
        for s in ("opus_high_pulumi_sealed_apidocs_T1_r1", "x", ""):
            want = int(subprocess.run(["cksum"], input=s.encode(),
                                      capture_output=True).stdout.split()[0])
            self.assertEqual(infra_base.cksum(s), want, s)

    def test_kind_cluster_name(self):
        t = infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs"))
        sub = t.make_context()
        self.assertEqual(sub.cluster_name, cluster_name_for(t.cid, "ecb-ka-"))
        self.assertEqual(sub.node, f"{sub.cluster_name}-control-plane")

    def test_sidecar_name(self):
        t = infra_for("pulumi_access_apidocs", fake_cell(variant="pulumi_access_apidocs"))
        sub = t.make_context()
        self.assertEqual(sub.container, f"fae-dind-{t.cid}")


class TestTheAgentAccess(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "cell"
        self.ws.mkdir()

    def test_sealed_split_arms_skip_cell_sandbox_and_hand_agent_nothing(self):
        with mock.patch.object(DockerContext, "ensure") as dock_ensure, \
                mock.patch.object(K8sContext, "ensure") as k8s_ensure:
            self.assertEqual(infra_for("pulumi_sealed_apidocs", fake_cell(ws=self.ws)).cell_setup(), {})
            self.assertEqual(infra_for("kedap_sealed_apidocs", fake_cell(variant="kedap_sealed_apidocs",
                                                                         ws=self.ws)).cell_setup(), {})
        dock_ensure.assert_not_called()
        k8s_ensure.assert_not_called()

    def test_pulumi_access_hands_the_agent_typed_docker_access(self):
        t = infra_for("pulumi_access_apidocs", fake_cell(variant="pulumi_access_apidocs", ws=self.ws))
        fake_sub = DockerContext(t.cid, scope="cell", in_verify=False, api_port=24123)
        with mock.patch.object(DockerContext, "ensure", return_value=fake_sub):
            acc = t.cell_setup()
        self.assertIsInstance(acc, AgentAccess)
        self.assertEqual(acc.env["DOCKER_HOST"], "tcp://host.docker.internal:24123")
        self.assertEqual((self.ws / "pulumi.env").read_text(),
                         f"PULUMI_DOCKER_HOST=tcp://fae-dind-{t.cid}:2375\n"
                         f"PULUMI_CACHE_HOST=fae-dind-{t.cid}\nPULUMI_CACHE_PORT=6379\n")

    def test_keda_access_hands_the_agent_typed_k8s_access(self):
        t = infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs", ws=self.ws))
        fake_sub = SimpleNamespace(
            cluster_name="ecb-ka-1",
            network="kind",
            kubeconfig=self.ws / "kubeconfig",
            host_kubeconfig=self.ws / "kubeconfig.host",
        )
        with mock.patch.object(K8sContext, "ensure", return_value=fake_sub), \
                mock.patch.object(keda, "_install_keda_stack", return_value="ecb"):
            acc = t.cell_setup()
        self.assertIsInstance(acc, AgentAccess)
        self.assertEqual(acc.network, "kind")
        self.assertEqual(acc.env["KUBECONFIG"], "/home/node/.kube/config")
        self.assertEqual(acc.mounts, ((str(self.ws / "kubeconfig"), "/home/node/.kube/config", "ro"),))
        self.assertEqual((self.ws / "kube.env").read_text(),
                         f"KUBECONFIG={self.ws}/kubeconfig.host\nKIND_CLUSTER=ecb-ka-1\n"
                         "NAMESPACE=ecb\n")


class TestTheShellEntryPointIsAShim(unittest.TestCase):
    def test_the_cli_prints_the_env_the_shell_side_evals(self):
        with mock.patch.object(tr, "infra_for") as inf:
            inf.return_value = SimpleNamespace(cell_setup=lambda: {"KUBE_MOUNT": "-e A=b"})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = tr.main(["kedap_access_apidocs", "setup", "cid", "/ws"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "KUBE_MOUNT='-e A=b'\n")


class TestTheDriverOwnsTheHookLifecycle(unittest.TestCase):
    def test_cell_delegates_and_maps_failure_to_rc_1(self):
        src = (Path(ENGINE_TREE) / "fae" / "cell" / "cell.py").read_text()
        self.assertIn("_variants.for_cell(self)", src)
        self.assertIn("except _variants.HookFailure:", src)
        self.assertNotIn("def hook(self", src)
        self.assertNotIn('"arm-lock queue"', src)


class TestTheKedaManifestImageParse(unittest.TestCase):
    def test_values_only_list_items_included_schema_keys_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            m = Path(d) / "keda.yaml"
            m.write_text("spec:\n  containers:\n  - image: ghcr.io/kedacore/keda:2.17.1\n"
                         "    name: keda\n  properties:\n    image:\n      type: string\n"
                         "  template:\n    image: \"ghcr.io/kedacore/keda-metrics-apiserver:2.17.1\"\n")
            self.assertEqual(keda.manifest_images(m),
                             {"ghcr.io/kedacore/keda:2.17.1",
                              "ghcr.io/kedacore/keda-metrics-apiserver:2.17.1"})


class TestKedaStackInstall(unittest.TestCase):
    def test_installs_keda_and_metrics_server_and_namespace(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "keda.yaml").write_text("image: ghcr.io/kedacore/keda:2.17.1\n")
            (d / "ms.yaml").write_text("image: registry.k8s.io/metrics-server:v0.7.2\n")
            calls = []

            class _FakeSub:
                cluster_name = "ecb-ka-1"

                def load_images(self, imgs):
                    calls.append(("load", list(imgs)))

                def apply(self, path, namespace=None, server_side=False):
                    calls.append(("apply", (Path(path).name, server_side)))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                def kubectl(self, *argv):
                    calls.append(("kubectl", list(argv)))
                    if argv[:2] == ("get", "crd") or argv[:4] == ("-n", "kube-system", "get", "deploy"):
                        return SimpleNamespace(returncode=1, stdout="", stderr="")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(keda, "_cached_manifest",
                                   side_effect=[d / "keda.yaml", d / "ms.yaml"]):
                ns = keda._install_keda_stack(_FakeSub(), d, lambda k, dv=None: dv, lambda m: None)
            self.assertEqual(ns, "ecb")
            self.assertIn(("apply", ("keda.yaml", True)), calls)
            self.assertIn(("apply", ("ms.yaml", False)), calls)
            rollouts = [a for kind, a in calls if kind == "kubectl" and "rollout" in a]
            self.assertEqual([r[-2] for r in rollouts], ["deploy/keda-operator", "deploy/metrics-server"])


if __name__ == "__main__":
    unittest.main()
