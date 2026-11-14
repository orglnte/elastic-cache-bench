"""What counts as a k8s cache backend for the clean-start / teardown guards.

The docs fix the Service name (`redis`); the Deployment behind it is named
by the agent. The guards must see it either way, and must ask the cell's
cluster — the one in the env they are handed — not whatever kubeconfig the
operator's shell happens to point at.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

from experiment.verifier import rig

RIG = (Path(ROOT) / "experiment" / "verifier" / "rig.py").read_text()


def _kubectl(deploys, svc_selector=None):
    """A fake `_run`: answers `kubectl get deploy -o json` and
    `kubectl get svc redis -o json` from the given shapes."""
    calls = []

    def run(argv, input_text=None, timeout=None, env=None):
        calls.append((argv, env))
        if argv[:3] == ["kubectl", "get", "deploy"]:
            items = [{"metadata": {"name": n},
                      "spec": {"template": {"metadata": {"labels": labels}}}}
                     for n, labels in deploys]
            return 0, json.dumps({"items": items}), ""
        if argv[:4] == ["kubectl", "get", "svc", "redis"]:
            if svc_selector is None:
                return 1, "", "not found"
            return 0, json.dumps({"spec": {"selector": svc_selector}}), ""
        return 0, "", ""
    run.calls = calls
    return run


class TestTheCacheDeploymentIsFoundByServiceSelector(unittest.TestCase):

    def test_a_deployment_named_redis_counts(self):
        with mock.patch.object(rig, "_run", _kubectl([("redis", {"app": "redis"})])):
            self.assertEqual(rig._k8s_cache_deployments("ns"), ["redis"])

    def test_a_deployment_behind_service_redis_counts_whatever_its_name(self):
        fake = _kubectl([("prometheus", {"app": "prometheus"}),
                         ("kv-store", {"app": "kv", "tier": "cache"})],
                        svc_selector={"app": "kv", "tier": "cache"})
        with mock.patch.object(rig, "_run", fake):
            self.assertEqual(rig._k8s_cache_deployments("ns"), ["kv-store"])

    def test_a_partial_selector_match_does_not_count(self):
        fake = _kubectl([("kv-store", {"app": "kv"})],
                        svc_selector={"app": "kv", "tier": "cache"})
        with mock.patch.object(rig, "_run", fake):
            self.assertEqual(rig._k8s_cache_deployments("ns"), [])

    def test_nothing_cache_shaped_means_clean(self):
        fake = _kubectl([("prometheus", {"app": "prometheus"})])
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            self.assertIsNone(rig.assert_no_cache_backend({"NAMESPACE": "ns"}))

    def test_the_leftover_names_the_deployments(self):
        fake = _kubectl([("redis-cache", {"app": "redis"})], svc_selector={"app": "redis"})
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            self.assertEqual(rig.assert_no_cache_backend({"NAMESPACE": "ns"}),
                             "k8s cache Deployment in ns/ns (redis-cache)")

    def test_force_clean_deletes_what_it_found(self):
        fake = _kubectl([("redis-cache", {"app": "redis"})], svc_selector={"app": "redis"})
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            rig.force_clean_cache_backend({"NAMESPACE": "ns"})
        deletes = [a for a, _ in fake.calls if a[:2] == ["kubectl", "delete"]]
        self.assertEqual(deletes, [["kubectl", "delete", "deploy/redis-cache", "svc/redis",
                                    "-n", "ns", "--ignore-not-found", "--wait=true"]])


class TestForceCleanReachesInsideThePulumiSandbox(unittest.TestCase):
    """A pulumi_access agent can start containers in its own dind (a cache
    it probed with, published on the sidecar's 6379); the clean-start guard
    sees that redis on the cache mapping, and force-clean must remove it
    the way teardown.sh does — or the attempt is voided for residue the
    agent, not the rig, left."""

    def _docker(self, inner):
        calls = []

        def run(argv, input_text=None, timeout=None, env=None):
            calls.append(argv)
            if argv[:2] == ["docker", "-H"] and argv[3:5] == ["ps", "-aq"]:
                return 0, "\n".join(inner), ""
            if argv[:3] == ["kubectl", "get", "deploy"]:
                return 0, json.dumps({"items": []}), ""
            return 0, "", ""
        run.calls = calls
        return run

    def test_inner_containers_are_removed_through_the_cells_endpoint(self):
        fake = self._docker(["b1343e9490d1", "63e610a3749b"])
        said = []
        env = {"NAMESPACE": "ns", "PULUMI_DOCKER_HOST": "tcp://127.0.0.1:24120"}
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            rig.force_clean_cache_backend(env, said.append)
        self.assertIn(["docker", "-H", "tcp://127.0.0.1:24120", "rm", "-f",
                       "b1343e9490d1", "63e610a3749b"], fake.calls)
        self.assertTrue(any("inside" in m for m in said), said)

    def test_an_empty_sandbox_is_not_touched(self):
        fake = self._docker([])
        env = {"NAMESPACE": "ns", "PULUMI_DOCKER_HOST": "tcp://127.0.0.1:24120"}
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            rig.force_clean_cache_backend(env)
        self.assertFalse([a for a in fake.calls if "rm" in a])

    def test_a_cell_without_a_sandbox_asks_no_docker(self):
        fake = self._docker(["deadbeef0000"])
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            rig.force_clean_cache_backend({"NAMESPACE": "ns"})
        self.assertFalse([a for a in fake.calls if a[:2] == ["docker", "-H"]])


class TestTheGuardsAskTheCellsCluster(unittest.TestCase):

    def test_kubectl_runs_with_the_env_it_was_handed(self):
        env = {"NAMESPACE": "ns", "KUBECONFIG": "/cell/kubecfg.host"}
        fake = _kubectl([("redis", {"app": "redis"})], svc_selector={"app": "redis"})
        with mock.patch.object(rig, "_run", fake), \
             mock.patch.object(rig, "_cache_containers", return_value=[]):
            rig.assert_no_cache_backend(env)
            rig.force_clean_cache_backend(env)
        kubectls = [(a, e) for a, e in fake.calls if a[0] == "kubectl"]
        self.assertTrue(kubectls)
        for argv, e in kubectls:
            self.assertIs(e, env, argv)


class TestTheProbeReadsAReadyEndpoint(unittest.TestCase):
    """cache_backend_ready (the sidecar's 1 Hz ground truth) is rig.py now."""

    def test_readiness_is_a_ready_endpoint_behind_service_redis(self):
        body = RIG[RIG.index("def cache_backend_ready("):]
        body = body[:body.index("\n\nif __name__")]
        self.assertIn("kubectl", body)
        self.assertIn("svc", body)
        self.assertIn("endpointslices", body)
        self.assertIn("kubernetes.io/service-name=redis", body)
        self.assertNotIn("get deploy redis", body)

    def test_the_leftover_guard_resolves_the_deployment_by_selector(self):
        # The leftover guard is the verify's (rig.py); the probe above is the
        # sidecar's. Both resolve the Deployment behind Service `redis` by
        # selector, never by a name the docs do not fix.
        src = (Path(ROOT) / "experiment" / "verifier" / "rig.py").read_text()
        self.assertIn("def _k8s_cache_deployments(", src)
        body = src[src.index("def assert_no_cache_backend("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("_k8s_cache_deployments(", body)
        self.assertNotIn("deploy redis", body)


if __name__ == "__main__":
    unittest.main()
