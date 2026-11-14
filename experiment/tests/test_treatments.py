"""The native variants — the fae/experiment/variants/ interface and the
experiment's variants (experiment/variants/).

Provisioning talks to docker and kind; a unit suite must not
need them, so the external commands are stubbed at the module's one seam
(`_run`/`_ok`) and the assertions are about what the treatment DECIDES:
ports, names, env files, the agent-container arguments, the failure shape,
idempotent teardown, and the shell shims being shims.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ENGINE_TREE, ROOT, infra_for

from fae.experiment import variants as tr
from fae.cell.infra import base as infra_base
from experiment import variants as sj
from fae.cell.infra import dind


def fake_cell(cid="opus_high_pulumi_sealed_apidocs_T1_r1", variant="pulumi_sealed_apidocs",
              root=None, ws=None):
    return SimpleNamespace(cid=cid, variant=variant,
                           root=root or ROOT, ws=ws or Path("/nonexistent") / cid,
                           conf=SimpleNamespace(get=lambda k, d=None: d))


class TestTheRegistry(unittest.TestCase):

    def test_every_variant_has_the_infra_of_its_tech(self):
        from _ctx import tech_of
        self.assertEqual(len(tr.registry()), 6)
        for vid, cls in tr.registry().items():
            self.assertEqual(cls.ID, vid)
            self.assertEqual(tech_of(cls), "keda" if vid.startswith("keda") else vid.split("_")[0])
            self.assertEqual(cls.FACTORS["tech"], tech_of(cls))

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


class TestPortsAndNamesAreTheShellsExactly(unittest.TestCase):
    """cksum-hashed, so a resumed cell finds its own sidecar and cluster."""

    def test_cksum_is_posix_cksum(self):
        for s in ("opus_high_pulumi_sealed_apidocs_T1_r1", "x", ""):
            want = int(subprocess.run(["cksum"], input=s.encode(),
                                      capture_output=True).stdout.split()[0])
            self.assertEqual(infra_base.cksum(s), want, s)


    def test_pulumi_ports_and_inner_cache_port(self):
        t = infra_for("pulumi_sealed_apidocs", fake_cell(variant="pulumi_sealed_apidocs"))
        h = infra_base.cksum(t.cid) % 800
        api, cache = t.ports()["api"], t.ports()["cache"]
        self.assertEqual((api, cache), (23800 + h, 24600 + h))
        self.assertEqual(t.sidecar(api, cache).inner_cache_port, 6379)

    def test_kind_cluster_name(self):
        c = sj.KindCluster(infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs")))
        self.assertEqual(c.name, f"ecb-ka-{infra_base.cksum(c.cid)}")
        self.assertEqual(c.node, f"{c.name}-control-plane")

    def test_sidecar_name_and_label(self):
        s = infra_for("pulumi_sealed_apidocs", fake_cell()).sidecar(25400, 25401)
        self.assertEqual(s.name, "fae-dind-opus_high_pulumi_sealed_apidocs_T1_r1")


class TestTheAgentEnvIsWhatTheHooksPrinted(unittest.TestCase):
    """The docker arguments the agent container receives, arm by arm."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "cell"
        self.ws.mkdir()

    def test_sealed_arms_hand_the_agent_nothing(self):
        with mock.patch.object(sj.DindSidecar, "ensure", return_value=True):
            self.assertEqual(infra_for("pulumi_sealed_apidocs", fake_cell(ws=self.ws)).cell_setup(), {})
            self.assertEqual(infra_for("pulumi_sealed_apidocs", fake_cell(variant="pulumi_sealed_apidocs",
                                                       ws=self.ws)).cell_setup(), {})
        self.assertEqual(infra_for("kedap_sealed_apidocs", fake_cell(variant="kedap_sealed_apidocs",
                                                 ws=self.ws)).cell_setup(), {})


    def test_the_sidecar_joins_the_cell_network(self):
        s = infra_for("pulumi_sealed_apidocs", fake_cell()).sidecar(1, 2)
        self.assertEqual(s.network, "fae-net-opus_high_pulumi_sealed_apidocs_T1_r1")
        argvs = []
        with mock.patch.object(s, "exists", return_value=False), \
             mock.patch.object(s, "alive", return_value=True), \
             mock.patch.object(s, "seed"), \
             mock.patch.object(infra_base, "_ok", side_effect=lambda argv, **k: argvs.append(argv) or True):
            s.ensure()
        self.assertEqual(argvs[0][argvs[0].index("--network") + 1], s.network)

    def test_pulumi_access_hands_the_agent_its_sidecar(self):
        with mock.patch.object(sj.DindSidecar, "ensure", return_value=True):
            t = infra_for("pulumi_access_apidocs", fake_cell(variant="pulumi_access_apidocs", ws=self.ws))
            env = t.cell_setup()
        api = t.ports()["api"]
        self.assertEqual(env, {"KUBE_MOUNT": f"-e DOCKER_HOST=tcp://host.docker.internal:{api}"})
        # the cache as the verify reaches it: the sidecar's name, the inner port
        self.assertEqual((self.ws / "pulumi.env").read_text(),
                         f"PULUMI_DOCKER_HOST=tcp://fae-dind-{t.cid}:2375\n"
                         f"PULUMI_CACHE_HOST=fae-dind-{t.cid}\nPULUMI_CACHE_PORT=6379\n")


    def test_keda_access_hands_the_agent_its_cluster(self):
        with mock.patch.object(sj.KindCluster, "provision", return_value="ecb-ka-1"), \
             mock.patch.object(sj.KindCluster, "network", return_value="kind"):
            t = infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs", ws=self.ws))
            env = t.cell_setup()
        self.assertEqual(env["DOCKER_NET"], "--network kind")
        self.assertEqual(env["KUBE_MOUNT"],
                         f"-v {self.ws}/kubeconfig:/home/node/.kube/config:ro "
                         "-e KUBECONFIG=/home/node/.kube/config")
        self.assertEqual((self.ws / "kube.env").read_text(),
                         f"KUBECONFIG={self.ws}/kubeconfig.host\nKIND_CLUSTER=ecb-ka-1\n"
                         "NAMESPACE=ecb\n")


class TestTheSidecarLifecycle(unittest.TestCase):

    def sidecar(self):
        return sj.DindSidecar("c", 25400, 25401, 25401, "docker:27-dind",
                              "redis:7-alpine", lambda m: None, "pulumi")

    def test_a_live_sidecar_is_reused_not_restarted(self):
        s = self.sidecar()
        with mock.patch.object(s, "exists", return_value=True), \
             mock.patch.object(s, "alive", return_value=True), \
             mock.patch.object(infra_base, "_ok") as ok:
            self.assertTrue(s.ensure())
        ok.assert_not_called()

    def test_a_wedged_sidecar_is_removed_then_started(self):
        s = self.sidecar()
        calls = []
        with mock.patch.object(s, "exists", return_value=True), \
             mock.patch.object(s, "alive", side_effect=[False, True, True]), \
             mock.patch.object(s, "remove", side_effect=lambda: calls.append("rm")), \
             mock.patch.object(s, "seed", side_effect=lambda: calls.append("seed")), \
             mock.patch.object(infra_base, "_ok", side_effect=lambda argv, **k: calls.append(argv[:3]) or True):
            self.assertFalse(s.ensure())
        self.assertEqual(calls[0], "rm")
        self.assertEqual(calls[1], ["docker", "run", "-d"])
        self.assertEqual(calls[-1], "seed")

    def test_a_sidecar_that_never_answers_is_removed_and_the_hook_fails(self):
        s = self.sidecar()
        with mock.patch.object(s, "exists", return_value=False), \
             mock.patch.object(s, "alive", return_value=False), \
             mock.patch.object(s, "remove") as rm, \
             mock.patch.object(infra_base, "_ok", return_value=True), \
             mock.patch.object(dind.time, "sleep"):
            with self.assertRaises(tr.HookFailure):
                s.ensure()
        rm.assert_called_once()

    def test_teardown_is_idempotent(self):
        s = self.sidecar()
        with mock.patch.object(s, "exists", return_value=False), \
             mock.patch.object(infra_base, "_run") as run:
            s.remove()
        run.assert_not_called()




class TestTheShellEntryPointIsAShim(unittest.TestCase):

    def test_the_cli_prints_the_env_the_shell_side_evals(self):
        with mock.patch.object(tr, "for_cell") as fc, \
             mock.patch.object(tr, "_ShimCell") as sc:
            sc.return_value = SimpleNamespace(variant="")
            fc.return_value = SimpleNamespace(cell_setup=lambda: {"KUBE_MOUNT": "-e A=b"})
            import io, contextlib
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
        # the hook-side pause exit cannot arrive any more: no queue in setup
        self.assertNotIn('"arm-lock queue"', src)

    def test_release_slot_clears_the_arm_note_too(self):
        src = (Path(ENGINE_TREE) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("    def release_slot("):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("self.clear_holder_notes()", body)
        notes = src[src.index("    def clear_holder_notes("):]
        notes = notes[:notes.index("\n    def ", 10)]
        self.assertIn("clear_holder_notes(self.cid, self.arm)", notes)
        queues = (Path(ENGINE_TREE) / "fae" / "conduct" / "_queues.py").read_text()
        qnotes = queues[queues.index("    def clear_holder_notes("):]
        qnotes = qnotes[:qnotes.index("\n    def ", 10)]
        self.assertIn("self._pool_dir(lock)", qnotes)


class TestTheKedaManifestImageParse(unittest.TestCase):
    """The images to pre-load are the manifest's `image:` VALUES — the shell
    grep demanded a value. A CRD schema names `image:` as a property with no
    value, and a list item carries it as `- image:`; both must parse."""

    def test_values_only_list_items_included_schema_keys_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            m = Path(d) / "keda.yaml"
            m.write_text("spec:\n  containers:\n  - image: ghcr.io/kedacore/keda:2.17.1\n"
                         "    name: keda\n  properties:\n    image:\n      type: string\n"
                         "  template:\n    image: \"ghcr.io/kedacore/keda-metrics-apiserver:2.17.1\"\n")
            t = infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs", ws=Path(d)))
            self.assertEqual(sj.KindCluster(t).manifest_images(m),
                             {"ghcr.io/kedacore/keda:2.17.1",
                              "ghcr.io/kedacore/keda-metrics-apiserver:2.17.1"})

class TestTheVerifyClusterCarriesWhatTheDocsPromise(unittest.TestCase):
    """The keda docs: KEDA and metrics-server already installed. kind ships
    neither; provision installs both."""

    def test_provision_installs_metrics_server_after_keda(self):
        c = sj.KindCluster(infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs")))
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(d)]))
        oks, runs = [], []

        def ok(argv, **kw):
            oks.append(argv)
            return argv[:5] != ["kubectl", "-n", "kube-system", "get", "deploy"]

        def run(argv, **kw):
            runs.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(infra_base, "_ok", ok), mock.patch.object(infra_base, "_run", run), \
             mock.patch.object(c, "exists", return_value=True), \
             mock.patch.object(c, "manifest", return_value=d / "keda.yaml"), \
             mock.patch.object(c, "metrics_server_manifest", return_value=d / "ms.yaml"), \
             mock.patch.object(c, "load_images"):
            c.provision(str(d / "kubeconfig"))
        applies = [a for a in oks if a[:3] == ["kubectl", "apply", "-f"]]
        self.assertIn(["kubectl", "apply", "-f", str(d / "ms.yaml")], applies)
        patch = [a for a in runs if a[:5] == ["kubectl", "-n", "kube-system", "patch", "deploy"]]
        self.assertTrue(patch and "--kubelet-insecure-tls" in patch[0][-1])
        rollouts = [a for a in oks if "rollout" in a]
        self.assertEqual([a[-2] for a in rollouts], ["deploy/keda-operator", "deploy/metrics-server"])

    def test_an_installed_metrics_server_is_not_reapplied_but_still_waited_for(self):
        c = sj.KindCluster(infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs")))
        oks = []
        with mock.patch.object(infra_base, "_ok", lambda argv, **kw: oks.append(argv) or True):
            c.install_metrics_server({})
        self.assertEqual([a[:3] for a in oks], [["kubectl", "-n", "kube-system"]] * 2)
        self.assertNotIn("apply", [a[1] for a in oks])
        self.assertEqual(oks[-1][-2:], ["deploy/metrics-server", "--timeout=120s"])

    def test_a_reused_cluster_whose_metrics_server_never_settled_fails_provision(self):
        c = sj.KindCluster(infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs")))
        with mock.patch.object(infra_base, "_ok", lambda argv, **kw: "rollout" not in argv):
            with self.assertRaises(tr.HookFailure):
                c.install_metrics_server({})





class TestTheSandboxClusterIsProvisionedThroughTheImage(unittest.TestCase):
    """From the driver on the host, kind and kubectl run in the keda image
    (Infra.tool): none before the node runs on any network, every one
    after on the cluster's — the API server is reachable by name there."""

    def test_kind_and_kubectl_go_through_the_tool_on_the_clusters_network(self):
        t = infra_for("kedap_access_apidocs", fake_cell(variant="kedap_access_apidocs"))
        c = sj.KindCluster(t, via_image=True)
        calls = []

        def tool(argv, env=None, network=None, timeout=900):
            calls.append((argv[:3], network, (env or {}).get("KUBECONFIG")))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(t, "tool", tool), \
             mock.patch.object(c.cluster, "running", side_effect=[False, True, True]), \
             mock.patch.object(c.cluster, "network", return_value="kind"):
            c.cluster.exists()
            c.run(["kubectl", "get", "ns", "x"], env={"KUBECONFIG": "/k"})
            self.assertTrue(c.ok(["kubectl", "get", "crd", "y"], env={"KUBECONFIG": "/k"}))
        self.assertEqual(calls, [(["kind", "get", "clusters"], None, None),
                                 (["kubectl", "get", "ns"], "kind", "/k"),
                                 (["kubectl", "get", "crd"], "kind", "/k")])

    def test_the_access_infra_provisions_and_deletes_via_the_image(self):
        src = (Path(ROOT) / "experiment" / "variants" / "keda" / "__init__.py").read_text()
        body = src[src.index("    def cell_setup(self):"):src.index("    # --- the verify side")]
        self.assertEqual(body.count("KindCluster(self, via_image=True)"), 2)
        sweep = src[src.index("    def sweep(cls):"):src.index("    def alive")]
        self.assertIn("via_image=True", sweep)
