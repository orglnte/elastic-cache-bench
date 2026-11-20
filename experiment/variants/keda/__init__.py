"""The keda overlay: rides on FAE's K8sSubstrate (`lifecycle = "split"`).
Installs KEDA, metrics-server, and the experiment namespace into the cluster;
during verify, rewrites APP_HOST scrape targets, applies authored manifests,
enforces scaler bindings, and runs the Redis port-forward."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from fae.cell.infra import base
from fae.cell.infra.base import HookFailure
from fae.cell.substrate import DomainOverlay

from .. import service

HERE = Path(__file__).resolve().parent
NAMESPACE_KINDS = "deploy,sts,ds,svc,cm,secret,job,cronjob,pvc"
_PLACEHOLDER = re.compile(r"(^|[^A-Za-z0-9_]|%2F)APP_HOST([^A-Za-z0-9_]|$)")


def _cached_manifest(root, name, url, what, log):
    f = Path(root) / "tmp" / "cache" / name
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
    log(f"kind: caching {what} (one-time download)")
    if not base.ok(["curl", "-fsSL", "-o", str(f), url]):
        f.unlink(missing_ok=True)
        raise HookFailure(f"could not download {what}")
    return f


def manifest_images(manifest):
    """Every `image: <value>` in the manifest — the VALUES only."""
    found = set()
    for line in Path(manifest).read_text().splitlines():
        m = re.search(r"image: *(\S+)", line)
        if m:
            found.add(m.group(1).strip('"'))
    return found


def _install_keda_stack(substrate, root, cfg, log):
    keda_ver = cfg("KEDA_VERSION", "v2.17.1")
    keda_manifest = _cached_manifest(
        root, f"keda-{keda_ver.lstrip('v')}.yaml",
        f"https://github.com/kedacore/keda/releases/download/{keda_ver}/keda-{keda_ver.lstrip('v')}.yaml",
        f"KEDA manifest {keda_ver}", log,
    )
    ms_ver = cfg("METRICS_SERVER_VERSION", "v0.7.2")
    ms_manifest = _cached_manifest(
        root, f"metrics-server-{ms_ver.lstrip('v')}.yaml",
        f"https://github.com/kubernetes-sigs/metrics-server/releases/download/{ms_ver}/components.yaml",
        f"metrics-server manifest {ms_ver}", log,
    )
    images = manifest_images(keda_manifest)
    images.add(cfg("CACHE_IMAGE", "redis:7-alpine"))
    substrate.load_images(sorted(images))
    if substrate.kubectl("get", "crd", "scaledobjects.keda.sh").returncode != 0:
        log(f"kind: installing KEDA into {substrate.cluster_name} (cached manifest)")
        applied = False
        for i in range(1, 4):
            if substrate.apply(keda_manifest, server_side=True).returncode == 0:
                applied = True
                break
            log(f"kind: KEDA apply attempt {i} failed (bootstrap race?) — retrying in 5s")
            time.sleep(5)
        if not applied:
            raise HookFailure("KEDA apply failed after 3 attempts")
    if substrate.kubectl("-n", "keda", "rollout", "status",
                         "deploy/keda-operator", "--timeout=180s").returncode != 0:
        raise HookFailure(f"keda-operator never became ready in {substrate.cluster_name}")
    if substrate.kubectl("-n", "kube-system", "get", "deploy", "metrics-server").returncode != 0:
        log(f"kind: installing metrics-server into {substrate.cluster_name}")
        if substrate.apply(ms_manifest).returncode != 0:
            raise HookFailure(f"metrics-server apply failed in {substrate.cluster_name}")
        substrate.kubectl(
            "-n", "kube-system", "patch", "deploy", "metrics-server",
            "--type=json", "-p",
            '[{"op":"add","path":"/spec/template/spec/containers/0/args/-",'
            '"value":"--kubelet-insecure-tls"}]',
        )
    if substrate.kubectl("-n", "kube-system", "rollout", "status",
                         "deploy/metrics-server", "--timeout=120s").returncode != 0:
        raise HookFailure(f"metrics-server never became ready in {substrate.cluster_name}")
    ns = cfg("NAMESPACE", "ecb")
    if substrate.kubectl("get", "ns", ns).returncode != 0:
        substrate.kubectl("create", "namespace", ns)
    return ns


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
    """Rewrite every whole-word `APP_HOST` and `http://<host>:<port>/metrics`
    URL in authored manifests to `host`. Returns (rewritten, surviving)."""
    url = re.compile(rf"http://[A-Za-z0-9._-]+:{port}/metrics")
    rewritten, surviving = [], []
    for f in manifests(k8s):
        text = f.read_text(errors="replace")
        new = text
        for _ in range(2):
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
    """Why the applied ScaledObjects break the arm's scaler binding, or None."""
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
    """Wipe namespace workloads and wait for ClusterIP IPAddress release."""
    log(f"resetting ns={ns} (attempt isolation)")
    if base.ok(["kubectl", "get", "crd", "scaledobjects.keda.sh"], env=kenv):
        base.run(["kubectl", "delete", "scaledobjects.keda.sh", "--all", "-n", ns,
                  "--ignore-not-found", "--timeout=60s"], env=kenv)
    base.run(["kubectl", "delete", NAMESPACE_KINDS, "--all", "-n", ns,
              "--ignore-not-found", "--timeout=90s"], env=kenv)
    base.run(["kubectl", "delete", "pods", "--all", "-n", ns, "--ignore-not-found",
              "--timeout=60s", "--force", "--grace-period=0"], env=kenv)
    path = ('{range .items[?(@.spec.parentRef.namespace=="' + ns + '")]}{.metadata.name}{"\\n"}{end}')
    for _ in range(60):
        held = base.run(["kubectl", "get", "ipaddresses", "-o", f"jsonpath={path}"], env=kenv).stdout
        if not held.strip():
            break
        time.sleep(1)


class KedaOverlay(DomainOverlay):
    TAG = "conv"
    ENV_FILE = "kube.env"
    VERIFY_ADOPTS = ("NAMESPACE",)

    def __init__(self):
        self._pf = None

    def on_cell_setup(self, substrate, infra):
        if substrate is None:
            return None
        try:
            ns = _install_keda_stack(substrate, infra.root, infra.cfg, infra.log)
        except HookFailure as e:
            infra.fail(f"HALT[infra]: {infra.variant.ID} cluster provision failed for {infra.cid}: {e}")
        infra.record_env([
            ("KUBECONFIG", str(substrate.host_kubeconfig)),
            ("KIND_CLUSTER", substrate.cluster_name),
            ("NAMESPACE", ns),
        ])
        infra.log(f"{infra.variant.ID}: ephemeral cluster {substrate.cluster_name} ready for {infra.cid} "
                  f"on network {substrate.network} (keda lock slot held)")
        return None

    def on_cell_teardown(self, substrate, infra):
        if infra.env_file:
            infra.env_file.unlink(missing_ok=True)

    def on_verify_setup(self, substrate, ctx, env):
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        me = getattr(ctx, "verify_container", "") or env.get("FAE_VERIFY_CONTAINER", "")
        if not me:
            raise HookFailure("the keda bring-up runs only inside the verify container "
                              "(FAE_VERIFY_CONTAINER unset)")
        cfg = (lambda k, d=None: (ctx.conf.get(k) if ctx.conf else None) or env.get(k, d))
        ns = _install_keda_stack(substrate, ctx.root, cfg, b.log)
        kenv = dict(env, **substrate.env())
        if substrate.kubectl("get", "ns", ns).returncode != 0:
            raise HookFailure(f"namespace '{ns}' not found after the provision")
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
            r = substrate.apply(m, namespace=ns)
            b.log((r.stdout + r.stderr).strip())
            if r.returncode:
                raise service.Rejected(f"kubectl apply {m.name} failed")
        scaler = ctx.variant_def.PARAMS.get("scaler") if ctx.variant_def else None
        if scaler:
            r = substrate.kubectl("get", "scaledobjects.keda.sh", "-n", ns, "-o", "json")
            try:
                applied = json.loads(r.stdout) if r.returncode == 0 else {}
            except ValueError:
                applied = {}
            why = scaler_violation(applied, scaler)
            if why:
                b.log(f"REJECTED: {why}")
                raise service.Rejected(why)
            b.log(f"scaler binding holds: every trigger is `{scaler}`")
        self._pf = service.Restarting(
            ["kubectl", "port-forward", "--address", "0.0.0.0", "-n", ns,
             f"svc/{cfg('CACHE_SVC', 'redis')}", "6379:6379"],
            kenv, b.out / ".redis_pf.pid",
        ).start()
        dsn = env.get("DB_DSN") or (f"postgresql://postgres@{env.get('DB_HOST', '127.0.0.1')}:"
                                     f"{env.get('DB_PORT', '5432')}/{env.get('DB_NAME', 'metadata')}")
        b.log(f"starting app on :{b.port} (task={ctx.task}, store={dsn}, cache={me}:6379)")
        return service.Up(
            env={"DB_DSN": dsn, "CACHE_HOST": me, "CACHE_PORT": "6379"},
            networks=(substrate.network,),
            exported={"KUBECONFIG": str(substrate.kubeconfig)},
        )

    def not_ready(self, substrate, ctx, env):
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        ns = (ctx.conf.get("NAMESPACE") if ctx.conf else None) or env.get("NAMESPACE", "ecb")
        if substrate is not None:
            b.log(substrate.kubectl("get", "pods,svc,scaledobject", "-n", ns).stdout)

    def on_verify_teardown(self, substrate, ctx, env):
        b = service.Bringup(ctx, env, f"teardown-{self.TAG}")
        if self._pf is not None:
            self._pf.stop()
            self._pf = None
        else:
            service.stop_pid(b.out / ".redis_pf.pid", wait_s=2)
        b.log("torn down the app and the cache-scaling stack", "teardown.log")
