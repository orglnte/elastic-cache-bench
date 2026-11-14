"""The contract check: each arm's doc promises asked of the world after the
bring-up and after the teardown. External commands are stubbed at rig._run;
the assertions are about which promise a given world breaks.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT

from experiment.verifier import contract, rig
from types import SimpleNamespace

from experiment.variants.keda.verify import contract as _keda
from experiment.variants.pulumi.verify import contract as _pulumi
contracts = SimpleNamespace(KedaContract=_keda.KedaContract, PulumiContract=_pulumi.PulumiContract)


class FakeVerify:
    def __init__(self, d, cid="opus_high_kedap_access_apidocs_T1_r1", env=None):
        self.ws = Path(d) / cid
        self.artifacts = self.ws / "artifacts"
        self.out = self.ws / ".verify-out"
        self.applied = self.out / "run" / "k8s"       # the service's copy the bring-up applies
        (self.artifacts / "k8s").mkdir(parents=True)
        self.applied.mkdir(parents=True)
        self.cid = cid
        self._env = dict(env or {})

    def rigenv(self):
        return dict(self._env)


def fake_run(answers):
    """rig._run stub: `answers` maps a tuple prefix of argv to (rc, out)."""
    calls = []

    def run(argv, input_text=None, timeout=None, env=None):
        calls.append((tuple(argv), env))
        for prefix, (rc, out) in answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return rc, out, ""
        return 0, "", ""
    run.calls = calls
    return run


KEDA_OK = {
    ("kubectl", "get", "svc", "redis"): (0, "{}"),
    ("kubectl", "get", "deploy", "-n"): (0, json.dumps({"items": [
        {"metadata": {"name": "redis"}, "spec": {"template": {"metadata": {"labels": {"app": "redis"}}}}}]})),
    ("kubectl", "-n", "keda", "get", "deploy", "keda-operator"): (0, "1"),
    ("kubectl", "-n", "kube-system", "get", "deploy", "metrics-server"): (0, ""),
    ("kind", "get", "clusters"): (0, "ecb-ka-other\n"),
}


class TestTheKedaContract(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.v = FakeVerify(self._tmp.name, env={"NAMESPACE": "ecb", "KUBECONFIG": "/v/kc"})
        (self.v.applied / "redis.yaml").write_text("kind: Deployment\n")

    def test_a_documented_world_breaks_nothing(self):
        with mock.patch.object(rig, "_run", fake_run(KEDA_OK)):
            self.assertEqual(contracts.KedaContract(self.v).after_deploy(), [])

    def test_a_surviving_placeholder_is_the_rewrite_promise(self):
        (self.v.applied / "prom.yaml").write_text("- APP_HOST:8080\n")
        with mock.patch.object(rig, "_run", fake_run(KEDA_OK)):
            broken = contracts.KedaContract(self.v).after_deploy()
        self.assertEqual([b.evidence for b in broken], ["prom.yaml"])
        self.assertIn("rewrites every `APP_HOST`", broken[0].doc_ref)

    def test_an_unrelated_identifier_sharing_the_prefix_is_not_a_violation(self):
        # An agent's own APP_HOSTS variable (plural) is not the APP_HOST
        # placeholder the bring-up rewrites — a substring match would wrongly
        # flag it, exactly as deploy.sh's own word-boundary regex does not.
        (self.v.applied / "prom.yaml").write_text(
            'APP_HOSTS = ["host.docker.internal", "127.0.0.1"]\n')
        with mock.patch.object(rig, "_run", fake_run(KEDA_OK)):
            broken = contracts.KedaContract(self.v).after_deploy()
        self.assertEqual(broken, [])

    def test_no_service_redis_is_the_service_name_promise(self):
        answers = {**KEDA_OK, ("kubectl", "get", "svc", "redis"): (1, "")}
        with mock.patch.object(rig, "_run", fake_run(answers)):
            broken = contracts.KedaContract(self.v).after_deploy()
        self.assertTrue(any("must be named `redis`" in b.doc_ref for b in broken), broken)

    def test_a_service_selecting_no_deployment_is_the_scaled_deployment_promise(self):
        answers = {**KEDA_OK, ("kubectl", "get", "deploy", "-n"): (0, json.dumps({"items": []}))}
        with mock.patch.object(rig, "_run", fake_run(answers)):
            broken = contracts.KedaContract(self.v).after_deploy()
        self.assertTrue(any("behind Service `redis`" in b.doc_ref for b in broken), broken)

    def test_a_manifest_missing_from_the_cluster_is_the_apply_promise(self):
        answers = {**KEDA_OK, ("kubectl", "get", "-f"): (1, "")}
        with mock.patch.object(rig, "_run", fake_run(answers)):
            broken = contracts.KedaContract(self.v).after_deploy()
        self.assertTrue(any("applies every" in b.doc_ref and b.evidence == "redis.yaml" for b in broken), broken)

    def test_missing_keda_or_metrics_server_break_the_installed_promise(self):
        answers = {**KEDA_OK, 
            ("kubectl", "-n", "keda", "get", "deploy", "keda-operator"): (0, "0"),
            ("kubectl", "-n", "kube-system", "get", "deploy", "metrics-server"): (1, "")}
        with mock.patch.object(rig, "_run", fake_run(answers)):
            refs = [b.doc_ref for b in contracts.KedaContract(self.v).after_deploy()]
        self.assertTrue(any("**KEDA**" in r for r in refs), refs)
        self.assertTrue(any("**metrics-server**" in r for r in refs), refs)

    def test_kubectl_runs_with_the_verifys_env(self):
        run = fake_run(KEDA_OK)
        with mock.patch.object(rig, "_run", run):
            contracts.KedaContract(self.v).after_deploy()
        for argv, env in run.calls:
            self.assertEqual(env.get("KUBECONFIG"), "/v/kc", argv)

    def test_a_surviving_verify_cluster_breaks_the_clean_world_promise(self):
        from fae.cell.infra.base import cksum
        name = f"ecb-ka-{cksum(self.v.cid + '-verify')}"
        answers = {**KEDA_OK, ("kind", "get", "clusters"): (0, f"{name}\n")}
        with mock.patch.object(rig, "_run", fake_run(answers)):
            broken = contracts.KedaContract(self.v).after_teardown()
        self.assertEqual([b.evidence for b in broken], [name])
        with mock.patch.object(rig, "_run", fake_run(KEDA_OK)):
            self.assertEqual(contracts.KedaContract(self.v).after_teardown(), [])


class TestThePulumiContract(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.v = FakeVerify(self._tmp.name, cid="sonnet_high_pulumi_access_apidocs_T1_r1",
                            env={"PULUMI_DOCKER_HOST": "tcp://h:1", "PULUMI_CACHE_HOST": "h",
                                 "PULUMI_CACHE_PORT": "2", "SERVICE_PORT": "8080"})

    def test_a_non_empty_state_backend_breaks_the_emptied_promise(self):
        state = self.v.out / "secrun-scratch" / ".pulumi-state"
        (state / ".pulumi" / "stacks").mkdir(parents=True)
        (state / ".pulumi" / "stacks" / "cache.json").write_text("{}")
        broken = contracts.PulumiContract(self.v).before_deploy()
        self.assertTrue(any("state backend is emptied" in b.doc_ref for b in broken), broken)
        with mock.patch.object(rig, "_run", fake_run({})):
            after = contracts.PulumiContract(self.v).after_deploy()
        self.assertFalse(any("state backend" in b.doc_ref for b in after),
                         "a running service may write its own stack; only the pre-deploy probe judges the backend")

    def test_an_unreachable_docker_host_and_missing_endpoints_break_their_promises(self):
        self.v._env.pop("PULUMI_CACHE_PORT")
        with mock.patch.object(rig, "_run", fake_run({("docker", "-H", "tcp://h:1", "info"): (1, "")})):
            refs = [b.doc_ref for b in contracts.PulumiContract(self.v).after_deploy()]
        self.assertTrue(any("PULUMI_DOCKER_HOST" in r for r in refs), refs)
        self.assertTrue(any("PULUMI_CACHE_PORT" in r for r in refs), refs)

    def test_a_service_container_surviving_the_teardown_breaks_the_service_promise(self):
        with mock.patch.object(rig, "_run", fake_run({("docker", "ps", "-aq", "--filter"): (0, "c0ffee\n")})):
            broken = contracts.PulumiContract(self.v).after_teardown()
        self.assertTrue(any("service's container is still there" in b.what for b in broken), broken)

    def test_leftover_containers_break_the_teardown_promise(self):
        with mock.patch.object(rig, "_run", fake_run({("docker", "-H", "tcp://h:1", "ps", "-q"): (0, "a\nb\n")})):
            broken = contracts.PulumiContract(self.v).after_teardown()
        self.assertEqual([b.evidence for b in broken], ["2 container(s)"])

    def test_teardown_without_a_docker_host_never_asks_the_host_daemon(self):
        self.v._env.pop("PULUMI_DOCKER_HOST")
        calls = []
        def run(argv, **kw):
            calls.append(tuple(argv))
            return 0, "", ""
        with mock.patch.object(rig, "_run", run):
            broken = contracts.PulumiContract(self.v).after_teardown()
        self.assertTrue(any("PULUMI_DOCKER_HOST" in b.doc_ref for b in broken), broken)
        # the only daemon question is where the service's runner lives
        self.assertEqual([c for c in calls if c[:1] == ("docker",)],
                         [("docker", "ps", "-aq", "--filter", "name=^fae-secrun-" + self.v.cid + "$")])





class TestTheDocPins(unittest.TestCase):
    """Every doc_ref names a promise that is actually in the seed docs."""

    def test_every_layout_promise_quoted_by_the_contract_exists(self):
        seeds = Path(ROOT) / "experiment" / "variants"
        docs = {arm: "".join(p.read_text() for p in seeds.glob(f"*/seed/T*.{arm}*.project_layout.md"))
                for arm in ("keda", "pulumi")}
        pins = {
            "keda": ["rewrites every `APP_HOST`", "must be named `redis`", "**KEDA**",
                     "**metrics-server**", "clean world", "applies every `k8s/*.yaml`"],
            "pulumi": ["Pulumi state backend", "`PULUMI_DOCKER_HOST`", "`PULUMI_CACHE_PORT`",
                       "removes the provisioning substrate wholesale", "`SERVICE_PORT`"],
        }
        for arm, needles in pins.items():
            for n in needles:
                self.assertIn(n, docs[arm], f"{arm}: {n}")

    def test_an_unknown_variant_gets_the_empty_contract(self):
        v = FakeVerify(tempfile.mkdtemp())
        v.variant_id = "nope"
        self.assertEqual(contract.for_variant(v).after_deploy(), [])
        self.assertEqual(contract.for_variant(v).after_teardown(), [])


if __name__ == "__main__":
    unittest.main()
