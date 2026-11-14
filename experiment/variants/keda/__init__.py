"""The keda infra: kind tooling, a per-verify throwaway cluster every
arrangement is judged on, and, when the variant's agent has access, a
per-cell sandbox cluster the agent is admin of.

Three families share it and differ only in what the docs let the agent's
ScaledObjects use: `keda_*` leaves the scaler open, `kedap_*` is bound to
the `prometheus` scaler (the policy in PromQL) and `kedam_*` to the
`metrics-api` scaler (the policy in the app, KEDA polling its field), the
variant file's `[infra] params.scaler`. A bound variant's verify refuses a
build whose triggers use anything else."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

import fae.experiment
from fae.cell.infra import base
from fae.cell.infra.base import HookFailure, cksum
from fae.cell.infra.kind import Cluster

from .. import service

HERE = Path(__file__).resolve().parent
NAMESPACE_KINDS = "deploy,sts,ds,svc,cm,secret,job,cronjob,pvc"
OWNED_CLUSTER = ".verify-owned-cluster"
KUBECFG = ".kubecfg"
_PLACEHOLDER = re.compile(r"(^|[^A-Za-z0-9_]|%2F)APP_HOST([^A-Za-z0-9_]|$)")


class KindCluster:
    """An ephemeral kind cluster with KEDA, metrics-server and the namespace
    the docs promise: the agent's sandbox (per cell) or the verify's world
    (per arrangement), capped so a worst-case agent saturates its own node.
    The cluster itself is the engine's block (fae/cell/infra/kind.py).

    `kind` and `kubectl` run in this process inside the verify container,
    or — `via_image`, from the driver on a host that carries only docker —
    in a throwaway container of the keda image on the cluster's network."""

    PREFIX = "ecb-ka-"

    def __init__(self, infra, cid=None, via_image=False):
        self.t = infra
        self.cid = cid or infra.cid
        self.root = infra.root
        self.name = self.name_for(self.cid)
        runner = (self._tool_run, self._tool_ok) if via_image else (None, None)
        self.cluster = Cluster(self.name, log=infra.log, run=runner[0], ok=runner[1])
        self.run, self.ok = self.cluster.run, self.cluster.ok
        self.node = self.cluster.node

    def _tool_run(self, argv, env=None, **kw):
        # on the cluster's network once its node runs: `kind create` needs
        # none, every kubectl after it needs the API server by name
        net = self.cluster.network() if self.cluster.running() else None
        return self.t.tool(argv, env=env, network=net)

    def _tool_ok(self, argv, env=None, **kw):
        return self._tool_run(argv, env=env).returncode == 0

    @classmethod
    def name_for(cls, cid):
        return f"{cls.PREFIX}{cksum(cid)}"

    def exists(self):
        return self.cluster.exists()

    def sweep_stale(self):
        Cluster.sweep(self.PREFIX, run=self.run)

    def _cached(self, name, url, what):
        """A downloaded manifest, kept under <root>/tmp/cache. Inside a verify
        the root is read-only: a file already cached is read from there, and
        a missing one is downloaded into the verify's own directory."""
        f = self.root / "tmp" / "cache" / name
        if f.is_file() and f.stat().st_size:
            return f
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            (f.parent / ".w").touch()
            (f.parent / ".w").unlink()
        except OSError:
            f = Path(os.environ.get("FAE_VERIFY_OUT") or "/tmp") / "cache" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            if f.is_file() and f.stat().st_size:
                return f
        self.t.log(f"kind: caching {what} (one-time download)")
        if not base._ok(["curl", "-fsSL", "-o", str(f), url]):
            f.unlink(missing_ok=True)
            raise HookFailure(f"could not download {what}")
        return f

    def manifest(self):
        version = self.t.cfg("KEDA_VERSION", "v2.17.1")
        return self._cached(f"keda-{version.lstrip('v')}.yaml",
                            f"https://github.com/kedacore/keda/releases/download/"
                            f"{version}/keda-{version.lstrip('v')}.yaml",
                            f"KEDA manifest {version}")

    @staticmethod
    def manifest_images(manifest):
        """Every `image: <value>` in the manifest — the VALUES only: a CRD
        schema names `image:` as a bare property key, which is no image."""
        found = set()
        for line in Path(manifest).read_text().splitlines():
            m = re.search(r"image: *(\S+)", line)
            if m:
                found.add(m.group(1).strip('"'))
        return found

    def metrics_server_manifest(self):
        version = self.t.cfg("METRICS_SERVER_VERSION", "v0.7.2")
        return self._cached(f"metrics-server-{version.lstrip('v')}.yaml",
                            f"https://github.com/kubernetes-sigs/metrics-server/releases/download/"
                            f"{version}/components.yaml",
                            f"metrics-server manifest {version}")

    def install_metrics_server(self, kenv):
        """The docs promise metrics-server on the cluster (`kubectl top`,
        resource-metric triggers); kind ships none. The kubelet's serving
        cert is self-signed, so the server must skip TLS verification."""
        if not self.ok(["kubectl", "-n", "kube-system", "get", "deploy", "metrics-server"], env=kenv):
            self.t.log(f"kind: installing metrics-server into {self.name}")
            if not self.ok(["kubectl", "apply", "-f", str(self.metrics_server_manifest())], env=kenv):
                raise HookFailure(f"metrics-server apply failed in {self.name}")
            self.run(["kubectl", "-n", "kube-system", "patch", "deploy", "metrics-server",
                      "--type=json", "-p",
                      '[{"op":"add","path":"/spec/template/spec/containers/0/args/-",'
                      '"value":"--kubelet-insecure-tls"}]'], env=kenv)
        # A deployment that exists may still be the one whose rollout timed
        # out last time; readiness is proven every provision, not once.
        if not self.ok(["kubectl", "-n", "kube-system", "rollout", "status",
                        "deploy/metrics-server", "--timeout=120s"], env=kenv):
            raise HookFailure(f"metrics-server never became ready in {self.name}")

    def load_images(self, manifest):
        images = self.manifest_images(manifest)
        images.add(self.t.cfg("CACHE_IMAGE", "redis:7-alpine"))
        self.cluster.load_images(images)

    def provision(self, out, container=None):
        """Create (or resume) the cluster with KEDA, metrics-server and the
        namespace; `out` gets the internal kubeconfig (the agent's, and the
        one every step here uses: from a tool container or the verify
        container — which joins the cluster's network first — the loopback
        port kind publishes on the host is out of reach), `out`.host the
        operator's. Returns the cluster name."""
        manifest = self.manifest()
        hostcfg = Path(f"{out}.host")
        self.cluster.create_or_resume(hostcfg)
        if container:
            self.cluster.join(container)
        self._write_internal(out)
        kenv = dict(os.environ, KUBECONFIG=str(out))
        mem = self.t.cfg("KEDA_ACCESS_MEM", "4g")
        # a verify's cluster is part of the measured world; an agent's sandbox is not
        pin = "CPUSET_MEASURED" if str(self.cid).endswith("-verify") else "CPUSET_AGENT"
        self.cluster.cap(self.t.cfg("KEDA_ACCESS_CPU", "1"), mem,
                         cpuset=self.t.cfg(pin, "") or os.environ.get(pin) or None)
        self.load_images(manifest)
        if not self.ok(["kubectl", "get", "crd", "scaledobjects.keda.sh"], env=kenv):
            self.t.log(f"kind: installing KEDA into {self.name} (cached manifest)")
            applied = False
            for i in range(1, 4):
                if self.ok(["kubectl", "apply", "--server-side", "-f", str(manifest)], env=kenv):
                    applied = True
                    break
                self.t.log(f"kind: KEDA apply attempt {i} failed (bootstrap race?) — retrying in 5s")
                time.sleep(5)
            if not applied:
                raise HookFailure("KEDA apply failed after 3 attempts")
        if not self.ok(["kubectl", "-n", "keda", "rollout", "status",
                        "deploy/keda-operator", "--timeout=180s"], env=kenv):
            raise HookFailure(f"keda-operator never became ready in {self.name}")
        self.install_metrics_server(kenv)
        ns = self.t.cfg("NAMESPACE", "ecb")
        if not self.ok(["kubectl", "get", "ns", ns], env=kenv):
            self.run(["kubectl", "create", "namespace", ns], env=kenv)
        return self.name

    def _write_internal(self, out):
        old = os.umask(0o077)
        try:
            Path(out).write_text(self.cluster.kubeconfig(internal=True))
        finally:
            os.umask(old)

    def network(self):
        return self.cluster.network()

    def delete(self):
        self.cluster.delete()


# --- the authored manifests ----------------------------------------------------

def manifests(k8s):
    """The manifest files under k8s/, sorted — never a README kubectl would
    choke on."""
    k8s = Path(k8s)
    if not k8s.is_dir():
        return []
    return sorted(p for p in k8s.rglob("*")
                  if p.is_file() and p.suffix in (".yaml", ".yml", ".json"))


def rewrite_scrape_targets(k8s, host, port):
    """`APP_HOST` is the documented placeholder for the host the app runs on,
    in whatever form the manifests spell it: a bare Prometheus target
    `APP_HOST:8080`, a full URL, an exporter's env value, a URL-encoded
    query parameter. Every whole-word occurrence in every authored manifest
    becomes `host`, and so does the host of any http://<host>:<port>/metrics
    URL. Returns (rewritten, surviving) file names, relative to k8s/."""
    url = re.compile(rf"http://[A-Za-z0-9._-]+:{port}/metrics")
    rewritten, surviving = [], []
    for f in manifests(k8s):
        text = f.read_text(errors="replace")
        new = text
        for _ in range(2):      # adjacent occurrences share the delimiter the first pass consumed
            new = _PLACEHOLDER.sub(lambda m: f"{m.group(1)}{host}{m.group(2)}", new)
        new = url.sub(f"http://{host}:{port}/metrics", new)
        rel = str(f.relative_to(k8s))
        if new != text:
            f.write_text(new)
            rewritten.append(rel)
        if _PLACEHOLDER.search(new):
            surviving.append(rel)
    return rewritten, surviving


def scaler_violation(scaledobjects, scaler):
    """Why the applied ScaledObjects break the arm's scaler binding, or None.

    `scaledobjects` is `kubectl get scaledobjects -o json`; every trigger of
    every ScaledObject must be of type `scaler`, and there must be one."""
    items = scaledobjects.get("items") or []
    if not items:
        return f"no ScaledObject in the namespace; this arm scales with the `{scaler}` scaler"
    wrong = sorted({f"{(so.get('metadata') or {}).get('name', '?')}:{t.get('type', '?')}"
                    for so in items
                    for t in ((so.get("spec") or {}).get("triggers") or [])
                    if t.get("type") != scaler})
    if wrong:
        return (f"this arm scales only with the `{scaler}` scaler; triggers of another "
                f"type: {', '.join(wrong)}")
    if not any((so.get("spec") or {}).get("triggers") for so in items):
        return f"no ScaledObject has a trigger; this arm scales with the `{scaler}` scaler"
    return None


def reset_namespace(ns, kenv, log):
    """The namespace is created EMPTY; everything in it belongs to a
    previous attempt and collides with re-authored manifests on immutable
    fields and on ScaledObject ownership. Wipe and WAIT: ScaledObjects first
    (releases KEDA's ownership), then the workloads, then the ClusterIPs
    the Services held — released asynchronously (IPAddress objects), and a
    manifest that pins one re-applied inside that window is rejected."""
    log(f"resetting ns={ns} (attempt isolation)")
    if base._ok(["kubectl", "get", "crd", "scaledobjects.keda.sh"], env=kenv):
        base._run(["kubectl", "delete", "scaledobjects.keda.sh", "--all", "-n", ns,
                   "--ignore-not-found", "--timeout=60s"], env=kenv)
    base._run(["kubectl", "delete", NAMESPACE_KINDS, "--all", "-n", ns,
               "--ignore-not-found", "--timeout=90s"], env=kenv)
    base._run(["kubectl", "delete", "pods", "--all", "-n", ns, "--ignore-not-found",
               "--timeout=60s", "--force", "--grace-period=0"], env=kenv)
    path = ('{range .items[?(@.spec.parentRef.namespace=="' + ns + '")]}{.metadata.name}{"\\n"}{end}')
    for _ in range(60):
        held = base._run(["kubectl", "get", "ipaddresses", "-o", f"jsonpath={path}"], env=kenv).stdout
        if not held.strip():
            break
        time.sleep(1)


class Keda(service.ServiceInfra):
    """The keda infra. params.scaler, when the variant names one, is the one
    KEDA trigger type its ScaledObjects may use."""
    TAG = "conv"
    # kube.env names the agent's sandbox cluster: the verify adopts only the
    # namespace and provisions a cluster of its own
    ENV_FILE = "kube.env"
    VERIFY_ADOPTS = ("NAMESPACE",)
    PREFIXES = {"cluster": KindCluster.PREFIX}

    @classmethod
    def identities(cls, cid):
        # the sandbox is a cell's only when its variant has access; naming it
        # for every cell costs a reaper one lookup of a cluster that is not there.
        # The per-verify cluster is the cell's too, so the host-load recorder
        # attributes it and the reaper removes one its verify left behind.
        return [("cluster", KindCluster.name_for(cid)),
                ("cluster", KindCluster.name_for(f"{cid}-verify"))]

    @classmethod
    def sweep(cls):
        """Stale sandbox and verify clusters, through the keda image (any
        variant of this infra names it): the operator's host carries no kind
        of its own."""
        from fae import paths
        from fae.experiment.variants import _ShimCell
        variant = next((v for v in fae.experiment.exp().definition.variants.values()
                        if v.INFRA is cls), None)
        if variant is None:
            return
        with tempfile.TemporaryDirectory(prefix="fae-sweep-") as ws:
            cell = _ShimCell("sweep", ws, paths.root())
            cell.variant = variant.ID
            KindCluster(cls(variant, cell), via_image=True).sweep_stale()

    def alive(self):
        # the per-verify cluster is created and deleted each arrangement;
        # what persists under it is the host daemon kind runs on
        return base.daemon_answers(["docker", "version"])

    def ok(self):
        if not base._ok(["docker", "info"]):
            self.log("HALT[infra]: docker unreachable")
            return False
        return True

    def cell_setup(self):
        """With access, the agent's own kind cluster for the cell's life,
        its container on the cluster's network; nothing otherwise, and the
        agent authors blind."""
        if not self.variant.ACCESS_INFRA:
            return {}
        kcfg = self.ws / "kubeconfig"
        # the driver on the host: kind and kubectl run in the keda image
        cluster = KindCluster(self, via_image=True)
        try:
            name = cluster.provision(kcfg)
            net = cluster.network()
        except HookFailure as e:
            self.fail(f"HALT[infra]: {self.variant.ID} cluster provision failed for {self.cid}: {e}")
        self.record_env([
            ("KUBECONFIG", f"{kcfg}.host"), ("KIND_CLUSTER", name),
            ("NAMESPACE", self.cfg("NAMESPACE", "ecb"))])
        self.log(f"{self.variant.ID}: ephemeral cluster {name} ready for {self.cid} "
                 f"on network {net} (keda lock slot held)")
        return {"DOCKER_NET": f"--network {net}",
                "KUBE_MOUNT": (f"-v {kcfg}:/home/node/.kube/config:ro "
                               f"-e KUBECONFIG=/home/node/.kube/config")}

    def cell_teardown(self):
        if not self.variant.ACCESS_INFRA:
            return
        for f in ("kubeconfig", "kubeconfig.host"):
            (self.ws / f).unlink(missing_ok=True)
        self.env_file.unlink(missing_ok=True)
        KindCluster(self, via_image=True).delete()

    # --- the verify side: one arrangement, inside the verify container ---

    def verify_setup(self, ctx, env):
        """A throwaway kind cluster of this arrangement's own, the authored
        cache-scaling stack applied to it, the cache port-forwarded to the
        service; the service, which the verifier starts after, joins the
        cluster's network so the cluster scrapes it by its runner's name,
        which `APP_HOST` becomes."""
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        me = env.get("FAE_VERIFY_CONTAINER")
        if not me:
            raise HookFailure("the keda bring-up runs only inside the verify container "
                              "(FAE_VERIFY_CONTAINER unset)")
        vcid = f"{ctx.cid or 'artifact-keda-' + ctx.task}-verify"
        # ownership is recorded BEFORE the provision: a cluster that came up
        # but never finished provisioning is still this verify's to tear down
        (b.out / OWNED_CLUSTER).write_text(f"{vcid}\n")
        kcfg = b.out / KUBECFG
        b.log(f"provisioning per-verify ephemeral cluster for {vcid}")
        cluster = KindCluster(self, cid=vcid)
        cluster.provision(kcfg, container=me)
        kenv = dict(env, KUBECONFIG=str(kcfg))
        ns = self.cfg("NAMESPACE", "ecb")
        if not base._ok(["kubectl", "get", "ns", ns], env=kenv):
            raise HookFailure(f"namespace '{ns}' not found after the provision")
        # the cluster scrapes the service in its runner, by the runner's name;
        # the rewrite goes into the service's copy, never the judged tree
        rewritten, surviving = rewrite_scrape_targets(b.copy / "k8s", b.host, b.port)
        b.log(f"cluster scrapes the app at {b.host}:{b.port}")
        for f in rewritten:
            b.log(f"  rewrote app scrape target -> {b.host} in k8s/{f}")
        if surviving:
            raise HookFailure("an APP_HOST placeholder survived the rewrite in "
                              f"k8s/{' k8s/'.join(surviving)} — the cluster cannot resolve it")
        if not rewritten:
            b.log(f"  NOTE: no APP_HOST placeholder or http://<host>:{b.port}/metrics "
                  "target found in any k8s manifest")
        reset_namespace(ns, kenv, b.log)
        # the infra is up; from here every failure is the agent's
        ms = manifests(b.copy / "k8s")
        if not ms:
            b.log("no manifests in k8s/ — nothing to apply (the app runs store-only "
                  "until a cache-scaling stack is declared)")
        else:
            b.log(f"kubectl apply ({len(ms)} manifest(s), ns={ns})")
        for m in ms:
            b.log(f"applying {m.relative_to(b.copy)}")
            r = base._run(["kubectl", "apply", "-f", str(m), "-n", ns], env=kenv)
            b.log((r.stdout + r.stderr).strip())
            if r.returncode:
                raise service.Rejected(f"kubectl apply {m.name} failed")
        scaler = self.variant.PARAMS.get("scaler")
        if scaler:
            r = base._run(["kubectl", "get", "scaledobjects.keda.sh", "-n", ns, "-o", "json"],
                          env=kenv)
            try:
                applied = json.loads(r.stdout) if r.returncode == 0 else {}
            except ValueError:
                applied = {}
            why = scaler_violation(applied, scaler)
            if why:
                b.log(f"REJECTED: {why}")
                raise service.Rejected(why)
            b.log(f"scaler binding holds: every trigger is `{scaler}`")
        # Redis is 0<->1: at 0 the forward drops (the app's cache probe
        # fails, which is correct); when the autoscaler scales it up the
        # forward reconnects
        self._pf = service.Restarting(
            ["kubectl", "port-forward", "--address", "0.0.0.0", "-n", ns,
             f"svc/{self.cfg('CACHE_SVC', 'redis')}", "6379:6379"],
            kenv, b.out / ".redis_pf.pid").start()
        dsn = env.get("DB_DSN") or (f"postgresql://postgres@{env.get('DB_HOST', '127.0.0.1')}:"
                                     f"{env.get('DB_PORT', '5432')}/{env.get('DB_NAME', 'metadata')}")
        b.log(f"starting app on :{b.port} (task={ctx.task}, store={dsn}, cache={me}:6379)")
        return service.Up(env={"DB_DSN": dsn, "CACHE_HOST": me, "CACHE_PORT": "6379"},
                          networks=(cluster.cluster.network(),),
                          exported={"KUBECONFIG": str(kcfg)})

    def not_ready(self, ctx, env):
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        kenv = dict(env, KUBECONFIG=str(b.out / KUBECFG))
        ns = self.cfg("NAMESPACE", "ecb")
        b.log(base._run(["kubectl", "get", "pods,svc,scaledobject", "-n", ns], env=kenv).stdout)

    def verify_teardown(self, ctx, env):
        """Stop the port-forward, then delete the cluster this verify owns —
        manifest-level cleanup subsumed — once the app is stopped."""
        b = service.Bringup(ctx, env, f"teardown-{self.TAG}")
        pf = getattr(self, "_pf", None)
        if pf is not None:
            pf.stop()
            self._pf = None
        else:
            service.stop_pid(b.out / ".redis_pf.pid", wait_s=2)
        marker = b.out / OWNED_CLUSTER
        if marker.exists():
            vcid = marker.read_text().strip()
            if vcid:
                b.log(f"deleting per-verify ephemeral cluster for {vcid}", "teardown.log")
                KindCluster(self, cid=vcid).delete()
            for f in (OWNED_CLUSTER, KUBECFG, f"{KUBECFG}.host"):
                (b.out / f).unlink(missing_ok=True)
        b.log("torn down the app and the cache-scaling stack", "teardown.log")
