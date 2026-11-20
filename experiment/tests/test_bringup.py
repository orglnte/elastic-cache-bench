"""The bring-ups' contracts with the docs handed to the agent.

The bring-ups are the variants' `verify_setup` / `verify_teardown`, run
inside the verify container; what the docs promise about them is what the
agent programs against. Each test here pins one promise to the code that
keeps it, or runs the piece itself on sample inputs.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT, infra_for

from fae.cell.infra import base
from fae.cell.verify import Ctx
from experiment.variants import keda, pulumi, service

ROOT = Path(ROOT)
SEEDS = ROOT / "experiment" / "variants"
SOURCES = {tech: (SEEDS / tech / "__init__.py").read_text() for tech in ("keda", "pulumi")}


def _method(src, name):
    body = src[src.index(f"    def {name}("):]
    return body[:body.index("\n    def ", 10)]


def fake_cell(cid="x_high_pulumi_sealed_apidocs_T1_r1", variant="pulumi_sealed_apidocs", ws=None):
    return SimpleNamespace(cid=cid, variant=variant, root=ROOT,
                           ws=ws or Path("/nonexistent") / cid,
                           conf=SimpleNamespace(get=lambda k, d=None: d))


def ctx_for(d, cid="x_high_pulumi_sealed_apidocs_T1_r1", variant="pulumi_sealed_apidocs"):
    ws = Path(d) / cid
    (ws / "artifacts").mkdir(parents=True, exist_ok=True)
    return Ctx(root=str(ROOT), experiment_dir=str(ROOT / "experiment"), workspace=str(ws),
               artifacts=str(ws / "artifacts"), out=str(ws), cid=cid, task="T1", variant=variant)


class TestTheKedaScrapeTargetRewrite(unittest.TestCase):
    """Docs: "write the host as APP_HOST … the bring-up rewrites every
    APP_HOST to the cluster-reachable host". The rewrite runs on every
    manifest in k8s/, exactly as verify_setup runs it."""

    def rewrite(self, files, host="10.9.8.7"):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for rel, text in files.items():
            p = d / "k8s" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        rewritten, surviving = keda.rewrite_scrape_targets(d / "k8s", host, "8080")
        return rewritten, surviving, {rel: (d / "k8s" / rel).read_text() for rel in files}

    def test_the_bare_prometheus_target_form_is_rewritten(self):
        _, left, out = self.rewrite({"prometheus.yaml":
                                     "static_configs:\n  - targets:\n    - APP_HOST:8080\n"})
        self.assertEqual(left, [])
        self.assertIn("- 10.9.8.7:8080\n", out["prometheus.yaml"])
        self.assertNotIn("APP_HOST", out["prometheus.yaml"])

    def test_the_url_form_and_an_exporter_env_value_are_rewritten(self):
        _, left, out = self.rewrite({
            "bridge.yaml": "env:\n  - name: TARGET\n    value: http://APP_HOST:8080/metrics\n",
            "exporter.yaml": "params:\n  target: [\"http%3A%2F%2FAPP_HOST%3A8080%2Fmetrics\"]\n",
            "other.yaml": "url: http://some-placeholder:8080/metrics\n"})
        self.assertEqual(left, [])
        self.assertIn("http://10.9.8.7:8080/metrics", out["bridge.yaml"])
        self.assertIn("%2F%2F10.9.8.7%3A8080", out["exporter.yaml"])
        self.assertIn("http://10.9.8.7:8080/metrics", out["other.yaml"])

    def test_adjacent_occurrences_and_json_manifests_are_covered(self):
        _, left, out = self.rewrite({"x.json": '{"a":"APP_HOST APP_HOST","b":"APP_HOST:8080"}\n'})
        self.assertEqual(left, [])
        self.assertNotIn("APP_HOST", out["x.json"])

    def test_a_word_that_merely_contains_app_host_is_left_alone(self):
        rewritten, left, out = self.rewrite({"cm.yaml": "name: MY_APP_HOSTNAME\nvalue: APP_HOST_X\n"})
        self.assertEqual((rewritten, left), ([], []))
        self.assertEqual(out["cm.yaml"], "name: MY_APP_HOSTNAME\nvalue: APP_HOST_X\n")

    def test_prose_naming_the_placeholder_is_not_a_surviving_placeholder(self):
        readme = "The bring-up rewrites `APP_HOST` to the host IP; target http://APP_HOST:8080/metrics.\n"
        rewritten, left, out = self.rewrite({"README.md": readme, "p.yaml": "- APP_HOST:8080\n"})
        self.assertEqual((rewritten, left), (["p.yaml"], []))
        self.assertEqual(out["README.md"], readme)
        self.assertNotIn("APP_HOST", out["p.yaml"])

    def test_the_rewrite_is_idempotent(self):
        _, _, out = self.rewrite({"p.yaml": "- APP_HOST:8080\n"})
        rewritten, left, out2 = self.rewrite({"p.yaml": out["p.yaml"]})
        self.assertEqual((rewritten, left), ([], []))
        self.assertEqual(out2["p.yaml"], out["p.yaml"])

    def test_the_verify_container_is_the_host_the_cluster_scrapes(self):
        """The service runs in the verify container, which joins the
        cluster's network: its own name is the address, and a bring-up
        outside a container has no address to give."""
        body = _method(SOURCES["keda"], "on_verify_setup")
        self.assertIn('FAE_VERIFY_CONTAINER', body)
        self.assertLess(body.index("rewrite_scrape_targets("), body.index("reset_namespace("))

    def test_the_docs_promise_the_form_the_rewrite_honours(self):
        """Every keda doc that names the placeholder tells the agent the
        bring-up rewrites `APP_HOST` itself — not one URL spelling of it."""
        for doc in sorted(SEEDS.glob("keda/seed/*.md")):
            text = doc.read_text()
            if "APP_HOST" not in text:
                continue
            self.assertIn("rewrites", text, doc.name)
            self.assertNotRegex(text, r"target as `APP_HOST:8080`", doc.name)




class TestTheKedaNamespaceResetReleasesPinnedClusterIPs(unittest.TestCase):
    """A Service's ClusterIP is released after the Service object is gone
    (IPAddress objects, k8s >= 1.33); a pinned clusterIP re-applied inside
    that window is rejected as already allocated."""

    def test_the_reset_waits_for_the_namespaces_ipaddresses(self):
        calls = []
        held = iter(["ip-1\n", "ip-1\n", ""])

        def run(argv, **kw):
            calls.append(argv)
            out = next(held) if argv[:3] == ["kubectl", "get", "ipaddresses"] else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        with mock.patch.object(base, "run", run), mock.patch.object(base, "ok", return_value=True), \
                mock.patch.object(keda.time, "sleep"):
            keda.reset_namespace("ns", {}, lambda m: None)
        kinds = [a for a in calls if a[:2] == ["kubectl", "delete"]]
        self.assertEqual([a[2] for a in kinds], ["scaledobjects.keda.sh", keda.NAMESPACE_KINDS, "pods"])
        waits = [a for a in calls if a[:3] == ["kubectl", "get", "ipaddresses"]]
        self.assertEqual(len(waits), 3)
        self.assertIn('parentRef.namespace=="ns"', waits[0][-1])
        self.assertLess(calls.index(kinds[-1]), calls.index(waits[0]))

    def test_the_reset_precedes_the_apply(self):
        body = _method(SOURCES["keda"], "on_verify_setup")
        self.assertLess(body.index("reset_namespace("), body.index("substrate.apply("))






class TestEveryBringUpClearsAStaleService(unittest.TestCase):
    """A runner left by a previous verify answers /health for the previous
    build; the verify would grade that one. The verifier removes it before
    any infra's bring-up, and the service it starts runs a fresh copy."""

    def test_the_runner_is_cleared_and_the_copy_fresh_before_the_bring_up(self):
        src = (ROOT / "experiment" / "verifier" / "__init__.py").read_text()
        body = _method(src, "deploy")
        self.assertLess(body.index("b.clear_stale()"), body.index("b.fresh_copy()"))
        self.assertLess(body.index("b.fresh_copy()"), body.index("infra.verify_setup("))
        self.assertLess(body.index("infra.verify_setup("), body.index("b.start_service("))
        for tech, src in SOURCES.items():
            self.assertNotIn("start_service(", src, tech)


class TestEveryKedaVerifyProvisionsItsOwnCluster(unittest.TestCase):
    """Both keda arms: the verify cluster is provisioned per verification
    through FAE's K8sSubstrate (`lifecycle = "split"`) with a "-verify" id."""

    def test_provisioning_is_unconditional_and_distinctly_named(self):
        from fae.cell.substrate import K8sContext
        t = infra_for("kedap_sealed_apidocs", fake_cell(variant="kedap_sealed_apidocs"))
        with tempfile.TemporaryDirectory() as d:
            ctx = ctx_for(d, cid="x_high_kedap_sealed_apidocs_T1_r1", variant="kedap_sealed_apidocs")
            sub = t.make_context(ctx)
        self.assertIsInstance(sub, K8sContext)
        self.assertTrue(sub.cluster_cid.endswith("-verify"))
        self.assertEqual(t.SPEC.lifecycle, "split")

    def test_the_teardown_deletes_the_owned_cluster_and_its_kubeconfig(self):
        from fae.cell.substrate import K8sContext
        with tempfile.TemporaryDirectory() as d:
            ctx = ctx_for(d, cid="x_high_kedap_sealed_apidocs_T1_r1", variant="kedap_sealed_apidocs")
            out = Path(ctx.out)
            (out / ".kubecfg").write_text("k")
            t = infra_for("kedap_sealed_apidocs", fake_cell(variant="kedap_sealed_apidocs"))
            ctx.substrate = t.make_context(ctx)
            deleted = []
            with mock.patch.object(service, "stop_pid"), \
                    mock.patch.object(ctx.substrate.cluster, "delete",
                                      lambda: deleted.append(ctx.substrate.cluster_cid)):
                t.verify_teardown(ctx, {})
            self.assertEqual(deleted, [f"{ctx.cid}-verify"])
            self.assertFalse((out / ".kubecfg").exists())


class TestThePulumiBringUp(unittest.TestCase):
    def test_the_service_provisions_into_the_verifys_own_daemon(self):
        from fae.cell.substrate import DockerContext
        with tempfile.TemporaryDirectory() as d:
            t = infra_for("pulumi_sealed_apidocs", fake_cell(variant="pulumi_sealed_apidocs"))
            ctx = ctx_for(d, variant="pulumi_sealed_apidocs")
            ctx.substrate = t.make_context(ctx)
            with mock.patch.object(DockerContext, "up", return_value=ctx.substrate):
                up = t.verify_setup(ctx, {"SERVICE_PORT": "8080"})
        self.assertEqual(up.env["DOCKER_HOST"], f"tcp://fae-vdind-{ctx.cid}:2375")

    def test_a_daemon_that_does_not_start_refuses(self):
        from fae.cell.substrate import DockerContext
        with tempfile.TemporaryDirectory() as d:
            t = infra_for("pulumi_sealed_apidocs", fake_cell(variant="pulumi_sealed_apidocs"))
            ctx = ctx_for(d, variant="pulumi_sealed_apidocs")
            ctx.substrate = t.make_context(ctx)
            with mock.patch.object(DockerContext, "up", side_effect=base.HookFailure("no")):
                with self.assertRaises(base.HookFailure):
                    t.verify_setup(ctx, {"SERVICE_PORT": "8080"})

    def test_the_teardown_clears_the_sidecars_inner_world_after_the_service(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = ctx_for(d, variant="pulumi_sealed_apidocs")
            t = infra_for("pulumi_sealed_apidocs", fake_cell(variant="pulumi_sealed_apidocs"))
            ctx.substrate = t.make_context(ctx)
            called = []
            with mock.patch.object(ctx.substrate, "reset", side_effect=lambda: called.append("reset")):
                t.verify_teardown(ctx, {})
            self.assertEqual(called, ["reset"])


class TestTheDocsStateThePerVerificationWorldReset(unittest.TestCase):
    """The rig resets the world per verification arrangement on every arm;
    the agent's docs say so, in each arm's own terms."""

    def test_every_project_layout_carries_the_sentence(self):
        docs = sorted(SEEDS.glob("*/seed/T*.project_layout.md"))
        self.assertEqual(len(docs), 6, "one layout per arm that has its own, the one task")
        for doc in docs:
            self.assertIn("Every verification starts from a clean world", doc.read_text(), doc.name)
        for doc in docs:
            if "pulumi" in doc.name:
                self.assertIn("Pulumi state backend", doc.read_text(), doc.name)
            if "_access" in doc.name and "keda" in doc.name:
                self.assertIn("sandbox cluster is neither used nor touched", doc.read_text(), doc.name)


if __name__ == "__main__":
    unittest.main()
