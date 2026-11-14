"""The experiment's verifier — one arrangement, deployed, loaded, judged,
torn down; `verify(ctx)` is what the engine runs (fae/cell/verify.py).

Ported from run_exp_artifact.sh (retired 2026-08-29). That script carried 34
mutable globals and shipped them to Python as environment variables at the
very end; every judgement it reached was already Python, so the bash was a
state-passing layer between two Python programs. Here the state is one object
and the judgement is a return value — a Verdict the engine reads.

`artifacts_dir` and `out_dir` are SEPARATE. In bash both were `$ws`, so a
re-verify could only ever run in the cell's own workspace — truncating
verify.log and k6.log and overwriting metrics.json and trace.csv, which are
three of the four files that constitute the recorded result. Splitting them
is what makes re-verifying a finished cell an ADDITION instead of a
replacement.

Every verify owns its store and its network (rig.cell_identity), so nothing
here needs an exclusive lock (ElasticCacheVerifier.EXCLUSIVE is None); the engine's
verify lock still runs one load test at a time for the load numbers' sake,
and it pins the fingerprint. Nothing here takes a lock. The store and the
cache-backend guards are experiment/verifier/rig.py; the world one
arrangement runs on is the variant's infra's (`verify_setup` /
`verify_teardown`, experiment/variants/<tech>/__init__.py), brought up
here, inside the verify container, and the service on it is the variant's
[verify.run], started and stopped here (experiment/variants/service.py).
"""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import fae.experiment
from fae.experiment import config as _config
from fae.cell.contrib import elastic_resource
from fae.cell.contrib.elastic_resource import calibration as _calibration
from fae.cell.contrib.elastic_resource.trace import first_request_epoch
from fae.cell.infra import secrunner
from fae.cell.infra.dind import VerifyDind
from fae.cell.infra.base import HookFailure
from fae.experiment.variants import _ShimCell
from fae.cell.verify import (ENGINE_INSTRUMENTS, Ctx, Verdict, Verifier, _now, call,
                             record_event, run_in_thread)

from .. import CONFIG
from ..variants import service
from . import contract
from . import rig

HERE = Path(__file__).resolve().parent          # the shared verify side: load profiles, rig
ROOT = HERE.parents[1]

# A failure of the rig, not of the build: the attempt is refunded.
VOID_STAGES = {"nostart", "store", "contract", "load-shape"}

# The per-arrangement evidence. Each file carries ONE arrangement, so they are
# truncated at the top of a run and archived under arrangements/ before the
# next invocation reuses the name — without that, five of every six are
# overwritten and no question about which arrangement was slow can be answered.
# the service log carries the app's own acquire markers and the provisioning
# spans — the only way to split mount latency into decision and
# provisioning when the trace exposes no decision epoch.
# What every verify of this experiment leaves, whatever its outcome: created
# empty at its start (prepare_out), filled by the stages that run. One missing
# after a verify is a rig defect, and the engine stands the cell down.
REQUIRED_OUTPUTS = ("verify.log", "deploy.log", "k6.log", "teardown.log",
                    "resource_sampler.log", "service.deploy.log", "sidecar.log",
                    "k6_summary.json", "schedule.json", "trace.csv", "trace_events.json",
                    "resources.json", "resources.csv")
ARRANGEMENT_FILES = ("verify.log", "k6.log", "deploy.log", "teardown.log",
                     "resource_sampler.log", "k6_summary.json",
                     "k6_results.jsonl", "k6_results.jsonl.gz",
                     "k6_blip_1.jsonl.gz", "k6_blip_2.jsonl.gz",
                     "trace.csv", "trace_events.json",
                     "schedule.json", "metrics.json",
                     "service.deploy.log")


def _now():
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def load_params(cfg):
    """The [load] keys of `cfg` (config values by key) as numbers, each
    missing one at its CONFIG default."""
    return {"baseline": int(cfg.get("LOAD_BASELINE_RPS") or 18),
            "top": int(cfg.get("LOAD_TOP_RPS") or 360),
            "ramp_s": int(cfg.get("LOAD_RAMP_S") or 20),
            "hold_s": str(cfg.get("LOAD_HOLD_S") or "").strip(),
            "blip_p99_ms": float(cfg.get("LOAD_BLIP_P99_MS") or 50),
            "blip_excess_req": int(cfg.get("LOAD_BLIP_EXCESS_REQ") or 10),
            "sat_p99_ms": float(cfg.get("LOAD_SAT_P99_MS") or 150),
            "max_mount_s": float(cfg.get("LOAD_MAX_MOUNT_S") or 30)}


class Result:
    """What one arrangement produced. `green` is the whole verdict; the rest is
    why."""

    def __init__(self):
        self.green = False
        self.shape = None
        self.stage_failed = ""
        self.deploy_ok = False
        self.e2e_pass = 0
        self.e2e_total = 0
        self.e2e_green = False
        self.load_ran = False
        self.k6_available = None      # null until asked: false must mean
        self.load_total = None        # "k6 missing", never "never checked"
        self.load_errors = None
        self.mount_delay_s = None
        self.ceiling_qps = None
        self.scaling_ok = None
        self.scaling_why = ""
        self.seconds = 0.0
        self.contract = []            # doc promises the world broke (rig fault)

    def to_metrics(self):
        """Every field, JSON-typed, in the metrics.json order: null means "never
        determined", never "determined and false"."""
        ceiling = self.ceiling_qps if isinstance(self.ceiling_qps, int) and self.ceiling_qps >= 0 else None
        return {
            "deploy_ok": bool(self.deploy_ok),
            "e2e_pass": int(self.e2e_pass), "e2e_total": int(self.e2e_total),
            "e2e_green": bool(self.e2e_green),
            "load_ran": bool(self.load_ran),
            "k6_available": self.k6_available,
            "load_total": self.load_total, "load_errors": self.load_errors,
            "mount_delay_s": None if self.mount_delay_s is None else float(self.mount_delay_s),
            "scaling_ok": self.scaling_ok,
            "scaling_why": self.scaling_why or None,
            "ceiling_qps": ceiling,
            "stage_failed": self.stage_failed or None,
        }

    @property
    def metrics(self):
        return {"e2e_pass": self.e2e_pass, "e2e_total": self.e2e_total,
                "e2e_green": self.e2e_green, "stage_failed": self.stage_failed,
                "scaling_ok": self.scaling_ok, "load_total": self.load_total,
                "load_errors": self.load_errors,
                "mount_delay_s": self.mount_delay_s}

    def __repr__(self):
        return (f"Result(green={self.green} shape={self.shape} "
                f"stage={self.stage_failed or '-'} "
                f"e2e={self.e2e_pass}/{self.e2e_total})")


class Verify:
    """One arrangement of the gate, for one artifacts directory."""

    def __init__(self, artifacts_dir, out_dir, task="T1", variant="",
                 cid="", shape=None, root=None, config=None, mode="cell", ctx=None):
        self.artifacts = Path(artifacts_dir)
        self.out = Path(out_dir)
        # The CELL's own directory, which is not the output directory during a
        # re-verify: the per-arm endpoint files and the skeleton manifest
        # belong to the cell and are read from there whatever `out_dir` says.
        self.ws = self.artifacts.parent
        self.mode = mode
        self.task = task
        self.variant_id = variant
        self.cid = cid or self.out.name
        self.shape = shape
        self.root = Path(root or os.environ.get("REPO_ROOT") or ROOT)
        self.conf = None
        self.cfg = config if config is not None else self.load_config()
        if self.conf is None:
            self.conf = _config.Config(self.cfg, {})
        self.experiment = Path(self.cfg.get("EXPERIMENT_DIR")
                               or self.root / _config.DEFAULT_EXPERIMENT_DIR)
        self.instruments = Path(self.cfg.get("INSTRUMENTS_DIR") or self.experiment / "instruments")
        # what the variant's pair is handed: the engine's ctx, or the same
        # facts assembled here for an artifact run
        self.ctx = ctx or Ctx(root=str(self.root), experiment_dir=str(self.experiment),
                              workspace=str(self.ws), artifacts=str(self.artifacts),
                              out=str(self.out), cid=self.cid, task=task, variant=variant,
                              arrangement=shape, mode=mode)
        self._infra = None
        self.result = Result()
        self.result.shape = shape
        # this verify's own store and network, then whatever the variant's
        # bring-up exports
        self._env = dict(rig.cell_identity(self.cid or "artifact"))

    def load_config(self):
        self.conf = _config.load(self.root)
        return self.conf.values

    def call(self, name, args=(), **kw):
        """An instrument of this experiment, run in-process (see `call`)."""
        return call(name, args, instruments=self.instruments, **kw)

    def block(self, name, args=(), **kw):
        """An instrument of the elastic_resource block (the load-shape law)."""
        return call(name, args, instruments=elastic_resource.DIR, **kw)

    # --- logging ----------------------------------------------------------

    def say(self, msg):
        line = f"{datetime.now(timezone.utc):%H:%M:%SZ}  {msg}\n"
        with (self.out / "verify.log").open("a") as f:
            f.write(line)
        sys.stderr.write(line)

    def _ledger(self, event, *fields):
        """A ledger event for the host to append after this verify returns
        (fae/cell/verify.py record_event): the verify never writes the ledger."""
        record_event(self.out, event, *fields)

    # --- http -------------------------------------------------------------

    @property
    def base_url(self):
        """The service in the cell's secure runner, reached by its name on
        the cell's network (variants/service.py)."""
        return f"http://{secrunner.name_for(self.cid)}:{self.cfg.get('SERVICE_PORT', '8080')}"

    def http(self, method, path, body=None, timeout=10):
        """Returns (status, text). A transport failure is a status of 0 — the
        assertions below care whether the contract held, not how it broke."""
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return 0, ""

    # --- stages -----------------------------------------------------------

    def adopt_cell_env(self):
        """The per-arm endpoints the cell's setup hook wrote.

        pulumi provisions the cache INSIDE the cell's own dind
        sidecar, invisible to host docker. Without these, the deploy, the
        cache probes and the teardown all target the wrong infra — the
        probe would report "no cache" forever, and the scaling gate would
        fail every cell of those arms for a reason that has nothing to do
        with the build.

        keda_access's kube.env names the agent's sandbox cluster; the verify
        never adopts it. Every keda verify provisions its own cluster in
        deploy.sh, so the sandbox is neither graded nor touched, and the
        guards' kubectl targets the verify cluster once deploy() adopts its
        kubeconfig.
        """
        infra = self.infra()
        adopted = infra.verify_env() if infra is not None else {}
        own = getattr(infra, "verify_endpoints", None)
        if own is not None:
            adopted = dict(adopted, **own(self.cid))
        self._env.update(adopted)
        return adopted

    def infra(self):
        """The infra of the variant this verify judges, instantiated for the
        cell (its cid, workspace, root, config); None in artifact mode."""
        if self._infra is None and self.variant_id:
            cls = fae.experiment.exp().definition.variant(self.variant_id)
            if cls is not None:
                cell = _ShimCell(self.cid, self.ws, self.root)
                cell.variant = self.variant_id
                self._infra = cls.INFRA(cls, cell)
        return self._infra

    def keda_warmup(self):
        """k8s pipeline warm-up. BEST-EFFORT by design.

        The per-attempt namespace reset restarts the whole metrics pipeline
        (Prometheus, exporter), and the deploy only gates on the app's /health.
        Without this, onset can race a cold pipeline and turn a slow pod start
        into a false scaling-law failure. Never fails the run: a broken
        manifest must surface as the scaling verdict it earns, not as a new rig
        failure mode. Names are agent-authored, so it waits on ALL deployments.
        """
        if not shutil.which("kubectl"):
            return
        ns = self._env.get("NAMESPACE") or self.cfg.get("NAMESPACE") or "ecb"
        self.say(f"readiness: waiting for k8s deployments in ns {ns} "
                 f"(best-effort, <=120s)")
        with (self.out / "deploy.log").open("a") as log:
            p = subprocess.run(["kubectl", "wait", "--for=condition=available",
                                "deploy", "--all", "-n", ns, "--timeout=120s"],
                               env=self.conf.child_env(self._env),
                               stdout=log, stderr=subprocess.STDOUT)
        self.say("readiness: k8s deployments available" if p.returncode == 0
                 else "readiness: WARN — some k8s deployments not available "
                      "after 120s (continuing; see deploy.log)")

    def capture_cluster_diag(self):
        """On a scaling FAIL in a keda run, snapshot the cluster-side data path
        BEFORE teardown deletes it.

        Without this a zero-mount failure cannot be separated into "KEDA never
        saw the metric" (scrape/exporter dead — a rig or wiring fault) from
        "KEDA saw it and the law was wrong" (authoring). Every probe is
        best-effort: a diagnostic that can fail the run is worse than no
        diagnostic.
        """
        if self.tech() != "keda" or not shutil.which("kubectl"):
            return
        d = self.out / "cluster-diag"
        d.mkdir(parents=True, exist_ok=True)
        self.say(f"cluster-diag: capturing KEDA/prometheus state -> {d}")
        env = self.conf.child_env(self._env)
        ns = self._env.get("NAMESPACE") or "ecb"

        def probe(path, *argv):
            try:
                with (d / path).open("w") as f:
                    subprocess.run(argv, env=env, stdout=f,
                                   stderr=subprocess.STDOUT, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass

        probe("keda-operator.log", "kubectl", "-n", "keda", "logs",
              "deploy/keda-operator", "--tail=300")
        probe("ns-objects.txt", "kubectl", "-n", ns, "get",
              "pods,svc,deploy,scaledobject", "-o", "wide")
        probe("scaledobject-describe.txt", "kubectl", "-n", ns, "describe",
              "scaledobject")
        try:
            pods = subprocess.run(["kubectl", "-n", ns, "get", "pods", "-o", "name"],
                                  env=env, capture_output=True, text=True,
                                  timeout=60).stdout.split()
        except (OSError, subprocess.SubprocessError):
            pods = []
        for pod in pods:
            probe(f"{pod.split('/')[-1]}.log", "kubectl", "-n", ns, "logs", pod,
                  "--tail=150", "--all-containers")

    def prepare_out(self):
        self.out.mkdir(parents=True, exist_ok=True)
        for f in REQUIRED_OUTPUTS:
            (self.out / f).write_text("")
        # The gz is this run's only persistent raw-k6 carrier: a run that fails
        # before the load would otherwise archive the previous arrangement's.
        for f in self.out.glob("k6_*.jsonl.gz"):
            f.unlink()

    def rigenv(self):
        """Config plus this run's exported variables — the one env view
        every rig helper reads."""
        return self.conf.child_env({"REPO_ROOT": self.root}, self._env)

    def own_containers(self):
        """The docker containers that are THIS cell's footprint: the harness
        store, the cell's dind sidecar, the access arm's daemon, the
        verify cluster's node and the service's runner. The sampler counts
        nothing else — other cells run beside this one."""
        from fae.cell.infra.base import cksum
        env = self.rigenv()
        own = [env["DB_CONTAINER"]]
        if self.tech() == "pulumi":
            own.append(VerifyDind.name_for(self.cid))
        if self.tech() == "keda":
            own.append(f"ecb-ka-{cksum(f'{self.cid}-verify')}-control-plane")
        own.append(secrunner.name_for(self.cid))
        return own

    def db_up(self):
        ok, exports, err = rig.db_up(self.rigenv(), self.say)
        self._env.update(exports)
        return ok, err

    MAX_REBENCHES = 3

    def bench_ceiling(self):
        ops = self._env.get("QUERY_CPU_OPS") or self.cfg.get("QUERY_CPU_OPS", "5000")
        return rig.db_bench_qps(self.rigenv(), ops)

    def deploy(self, tech):
        """The arrangement's world (the infra's `verify_setup`), then the
        service the variant's [verify.run] declares, over a fresh copy of
        the artifacts in the cell's secure runner, held to its entrypoint
        contract.

        What it raises is what makes a deploy failure classifiable:
        `Rejected` means the artifacts were REJECTED after the infra was
        up (authoring, scored); anything else means the rig never got the
        infra up (refunded, retried). Without the split, a broken
        manifest was refunded forever — and the refund also erased the
        failure from the agent's history, so it reproduced the same error
        every cycle.
        """
        infra = self.infra()
        if infra is None:
            return "missing"
        if tech == "pulumi":
            # The clean start removed every container in the cell's docker
            # host; the Pulumi state that described them goes with them, or
            # the program's next up/destroy acts on containers that no
            # longer exist.
            shutil.rmtree(self.out / service.SCRATCH / ".pulumi-state", ignore_errors=True)
        self.pre_deploy = contract.for_variant(self).before_deploy()
        if self.pre_deploy:
            return "contract"
        env = self.conf.child_env(
            {"REPO_ROOT": self.root,
             "SERVICE_PORT": self.cfg.get("SERVICE_PORT", "8080"),
             "TASK": self.task, "CELL_ID": self.cid},
            self._env)
        b = service.Bringup(self.ctx, env, f"deploy-{infra.TAG}")
        try:
            b.clear_stale()
            b.fresh_copy()
            up = infra.verify_setup(self.ctx, env) or service.Up()
            # the infra is up; from here every failure is the agent's
            b.start_service(infra.variant, up)
            try:
                b.service_ready()
            except service.Rejected:
                infra.not_ready(self.ctx, env)
                raise
        except service.Rejected as e:
            self._deploy_log(f"ERROR: {e}")
            return "rejected"
        except (HookFailure, OSError, subprocess.SubprocessError, RuntimeError) as e:
            self._deploy_log(f"ERROR: {e}")
            return "nostart"
        # what the bring-up exported (a kubeconfig): the guards and the
        # diagnostics ask the world the bring-up made, not the operator's
        self._env.update({k: str(v) for k, v in up.exported.items()})
        return "ok"

    def _deploy_log(self, line):
        with (self.out / "deploy.log").open("a") as log:
            log.write(line + "\n")

    def readiness(self):
        """First a 200, then the contract HOLDING. The old loop broke on the
        first 200, so a service that came up and died immediately passed it and
        the run was scored as an authoring failure."""
        timeout = int(self.cfg.get("VERIFY_READY_TIMEOUT_S") or 90)
        for _ in range(timeout):
            if self.http("GET", "/health")[0] == 200:
                break
            time.sleep(1)
        else:
            return 0, 0
        hold = int(self.cfg.get("VERIFY_READY_HOLD_S") or 10)
        held = 0
        for _ in range(hold):
            if self.http("GET", "/health")[0] != 200:
                break
            held += 1
            time.sleep(1)
        return held, hold

    def e2e(self):
        """The functional contract, per task. Every arm answers the same
        endpoints on the same port — that is the entrypoint contract — so this
        is variant-agnostic by construction."""
        r = self.result
        checks = []

        def check(name, ok):
            checks.append((name, bool(ok)))

        code, body = self.http("GET", "/health")
        check("GET /health 200", code == 200 and '"status"' in body)
        code, body = self.http("GET", "/metrics")
        check("GET /metrics exports pool_pressure",
              code == 200 and "pool_pressure" in body)
        code, body = self.http("GET", "/health")
        check("GET /health exposes cache_mounted", "cache_mounted" in body)

        if self.task == "T1":
            tok = f"rt{int(time.time())}{os.getpid()}"
            code, body = self.http("POST", "/photos",
                                   {"owner": "e2e",
                                    "caption": f"{tok} sunset over the beach"})
            pid = self._json_get(body, "id") if code == 201 else ""
            check("POST /photos 201", code == 201 and pid)
            if pid:
                code, body = self.http("GET", f"/photos/{pid}")
                check("GET /photos/{id} round-trips id+owner+caption",
                      code == 200 and self._json_get(body, "id") == pid
                      and '"owner"' in body and tok in body)
            else:
                check("GET /photos/{id} skipped (no id)", False)
            code, body = self.http("GET", "/photos?owner=e2e")
            check("GET /photos?owner lists posted id",
                  code == 200 and '"photos"' in body and str(pid) in body)
            code, body = self.http("GET", "/photos/search?q=sunset")
            check("GET /photos/search 200", code == 200 and '"photos"' in body)
        else:
            check(f"unknown TASK '{self.task}' — no e2e assertion set", False)

        for name, ok in checks:
            if not ok:
                self.say(f"  e2e FAIL: {name}")
        r.e2e_total = len(checks)
        r.e2e_pass = sum(1 for _, ok in checks if ok)
        r.e2e_green = r.e2e_total > 0 and r.e2e_pass == r.e2e_total
        self.say(f"e2e: {'GREEN' if r.e2e_green else 'NOT GREEN'} "
                 f"({r.e2e_pass}/{r.e2e_total})")
        if not r.e2e_green:
            r.stage_failed = "e2e"
        return r.e2e_green

    @staticmethod
    def _json_get(body, key):
        try:
            v = json.loads(body).get(key, "")
            return "" if v is None else str(v)
        except (ValueError, AttributeError):
            return ""

    def teardown(self, tech):
        env = self.conf.child_env({"REPO_ROOT": self.root}, self._env)
        infra = self.infra()
        if infra is not None:
            try:
                service.Bringup(self.ctx, env, f"teardown-{infra.TAG}").stop_service()
                infra.verify_teardown(self.ctx, env)
            except Exception as e:      # noqa: BLE001 — best effort by contract
                self.say(f"WARN: teardown raised {type(e).__name__}: {e}")
        rig.db_down(self.rigenv())
        if rig.assert_no_cache_backend(self.rigenv()) is None:
            self.say("teardown: verified — no cache backend remains")
        else:
            self.say("WARN: cache backend still present after teardown — force-cleaning")
            rig.force_clean_cache_backend(self.rigenv(), self.say)
        broken = contract.for_variant(self).after_teardown()
        for x in broken:
            # the verdict stands (the world was right while it was measured);
            # what survived the teardown would poison the NEXT arrangement
            self.say(f"CONTRACT[teardown]: {x}")
        self.result.contract += [f"teardown: {x}" for x in broken]
        down = getattr(infra, "verify_down", None)
        if down is not None:
            try:
                down(self.ctx)
            except Exception as e:      # noqa: BLE001 — best effort, the reaper follows
                self.say(f"WARN: verify_down raised {type(e).__name__}: {e}")

    @staticmethod
    def _gzip(path):
        """Keep the FULL per-request k6 data, compressed (~24MB -> ~2MB per
        verify). The stdlib module rather than the binary: one fewer child, and
        strictly post-measurement either way."""
        if not path.is_file() or not path.stat().st_size:
            return
        try:
            with path.open("rb") as src, gzip.open(f"{path}.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        except OSError:
            pass

    def arrangement_label(self):
        """The arrangement this run drove: the shape asked for, else the one
        the schedule recorded, else the seed."""
        if self.shape:
            return self.shape
        try:
            return json.loads((self.out / "schedule.json").read_text()).get("shape", "") or "seed"
        except (OSError, ValueError):
            return "seed"

    def tech(self):
        """The variant's tech: the package of its infra class (variants/<tech>)."""
        cls = fae.experiment.exp().definition.variant(self.variant_id) if self.variant_id else None
        return cls.INFRA.__module__.rsplit(".", 1)[-1] if cls is not None else ""

    # --- load -------------------------------------------------------------

    CANONICAL_SHAPE = "BBS"     # the arrangement a verify with no shape asked for drives

    def blips(self, sched, k6_jsonl, k6_alive, log):
        """Drive every blip of the schedule: at each blip window's t0,
        counted on the clock from the main generator's first request, a
        loadgen_blip.js of its own, ended by its stop rule or its ramp. A
        main generator that ends without a request gets no blips. Returns
        the k6 outputs the trace tails."""
        outs = []
        windows = [w for w in (sched or {}).get("events", []) if w["kind"] == "blip"]
        for i, w in enumerate(windows, 1):
            outs.append(self.out / f"k6_blip_{i}.jsonl")
            outs[-1].unlink(missing_ok=True)

        def drive():
            lp = self.load_params()
            zero = None
            while windows and zero is None:
                zero = first_request_epoch(k6_jsonl)
                if zero is None:
                    if not k6_alive():
                        log("blips: the main generator sent no request; none driven")
                        return
                    time.sleep(0.1)
            for i, w in enumerate(windows, 1):
                wait = zero + w["t0"] - time.time()
                if wait > 0:
                    time.sleep(wait)
                t = time.monotonic()
                with (self.out / "k6.log").open("a") as klog:
                    p = subprocess.Popen(
                        ["k6", "run", "--quiet", "--out", f"json={outs[i - 1]}",
                         str(HERE / "loadgen" / "loadgen_blip.js")],
                        # a blip stops at the first slow request, so a few
                        # hundred VUs cover it; the main generator's pool is
                        # sized for the spike's saturation window
                        env={**os.environ, "VU_PREALLOC": "200", "VU_MAX": "1000"},
                        stdout=klog, stderr=subprocess.STDOUT)
                    try:
                        rc = p.wait(timeout=lp["ramp_s"] + 15)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        rc = "killed"
                log(f"blip {i}: {time.monotonic() - t:.1f}s "
                    f"({'stopped by its rule' if rc == 108 else 'ran its whole ramp' if rc == 0 else f'rc={rc}'})")

        return outs, threading.Thread(target=drive, name="blips", daemon=True)

    def load_shape(self):
        """The arrangement to drive, and how long a window it needs.

        A seeded PERMUTATION of the same three events (blip, blip, spike), so
        an agent iterating on trace feedback cannot hardcode the timeline —
        only a signal-driven policy passes every arrangement. A stale schedule
        from a previous shaped run is removed first, or an unshaped run would
        be judged against the wrong law.
        """
        (self.out / "schedule.json").unlink(missing_ok=True)
        env = {"REPO_ROOT": self.root, **self.load_env()}
        if self.shape:
            env["VERIFY_SHAPE"] = self.shape
        elif not os.environ.get("VERIFY_SHAPE_SEED"):
            env["VERIFY_SHAPE"] = self.CANONICAL_SHAPE
        rc, stages = self.block("load_shape", [self.out], env=env)
        if rc != 0 or not stages:
            self.say("WARN: shape generation failed — falling back to the canonical shape")
            return "", None
        try:
            sched = json.loads((self.out / "schedule.json").read_text())
        except (OSError, ValueError):
            return stages, None
        return stages, sched

    def load_params(self):
        """The load shape the definition's [load] keys declare: the rates
        k6 drives (baseline, the top of the ramp both blips and spike reach),
        the ramp and hold lengths, the blip's stop rule (the p99 that means
        pressure, and how many requests over the knee it then offers), and
        the two numbers the law reads — the p99 at which the store counts as
        saturated and the mount deadline after it."""
        return load_params(self.cfg)

    def load_env(self):
        """The [load] keys as the block's instruments read them."""
        lp = self.load_params()
        env = {"DISC_BASELINE": str(lp["baseline"]), "DISC_SPIKE": str(lp["top"]),
               "DISC_T_RAMP": str(lp["ramp_s"]),
               "BLIP_TOP": str(lp["top"]), "BLIP_RAMP_S": str(lp["ramp_s"]),
               "BLIP_P99_MS": str(lp["blip_p99_ms"]),
               "BLIP_EXCESS": str(lp["blip_excess_req"]),
               "VERIFY_SAT_P99_MS": str(lp["sat_p99_ms"]),
               "VERIFY_MAX_MOUNT_S": str(lp["max_mount_s"])}
        if lp["hold_s"]:
            env["DISC_T_SPIKE"] = lp["hold_s"]
        return env

    @staticmethod
    def k6_counts(line):
        """(load_total, load_errors, mount_delay_s) from the k6 block's line,
        as numbers. Positional, and a short line is normal: the mount delay is
        empty when nothing mounted, and requiring all five fields would discard
        the load counts the scaling gate needs."""
        parts = line.split()

        def field(i, cast):
            if i >= len(parts):
                return None
            try:
                return cast(parts[i])
            except ValueError:
                return None
        return field(0, int), field(1, int), field(4, float)

    def run_load(self, ceiling_qps):
        """k6 in the BACKGROUND, the rich trace sidecar in the FOREGROUND.

        Nothing is killed by pid — k6's graceful stop outlives the scenario, so
        the sidecar ends on its own timer and k6 is waited for. None of these
        children is handed the rig lock; on this side that is the default
        rather than a wrapper somebody has to remember.
        """
        r = self.result
        if not shutil.which("k6"):
            r.k6_available = False
            self.say("k6: NOT INSTALLED — load metrics left null")
            return
        r.k6_available = True
        r.load_ran = True
        duration = int(self.cfg.get("VERIFY_CELL_K6_DURATION_S") or 100)
        stages, sched = self.load_shape()
        if sched:
            total = int(sched.get("total_s", 0)) + 5
            duration = max(duration, total)
            self.say(f"load shape: {sched.get('shape', '?')} — schedule.json "
                     f"written, window {duration}s")
        k6sum = self.out / "k6_summary.json"
        k6jsonl = self.out / "k6_results.jsonl"
        k6jsonl.unlink(missing_ok=True)
        trace_csv, trace_events = self.out / "trace.csv", self.out / "trace_events.json"
        loadgen = (self.cfg.get("LOADGEN_FILE")
                   or str(HERE / "loadgen" / "loadgen_discriminate.js"))
        env = self.conf.child_env({"BASE_URL": self.base_url, "TASK": self.task,
                   "DURATION_S": duration, **self.load_env(),
                   "VU_PREALLOC": self.cfg.get("VU_PREALLOC", "8000"),
                   "VU_MAX": self.cfg.get("VU_MAX", "12000")},
                  # The cache probe reaches the arm's sidecar through these; a
                  # mount the probe cannot confirm is not a mount.
                  self._env)
        if stages:
            env["DISC_STAGES"] = stages
        self.say(f"k6: profile {Path(loadgen).name}, window {duration}s "
                 f"against {self.base_url}")
        with (self.out / "k6.log").open("a") as klog:
            k6 = subprocess.Popen(
                ["k6", "run", "--quiet", "--summary-export", str(k6sum),
                 "--out", f"json={k6jsonl}", loadgen],
                env=env, stdout=klog, stderr=subprocess.STDOUT)
        blip_outs, blips = self.blips(sched, k6jsonl, lambda: k6.poll() is None, self.say)
        # The sampler thread and the sidecar overlap, and both their children
        # (docker, kubectl, the cache probe) read os.environ — so the load
        # window has ONE environment writer: it is set here, spans both
        # instruments, and is restored only after the sampler joins. Per-call
        # patching would interleave two save/restore pairs across threads and
        # resurrect the other context's variables into the cell.
        saved_env = dict(os.environ)
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            # A THREAD, not a child: the sampler has to overlap the load
            # window, and it is IO-bound (it shells out to docker and kubectl
            # and waits), so a thread is the right shape and the cell stays
            # one process.
            sampler = run_in_thread(
                "resource_sampler",
                ["--duration-s", duration + 5,
                 "--csv-output", self.out / "resources.csv",
                 "--json-output", self.out / "resources.json",
                 "--label", self.variant_id,
                 "--namespaces", f"{self.cfg.get('NAMESPACE') or 'ecb'},keda",
                 "--infra-namespaces", "keda,kube-system",
                 *[a for name in self.own_containers() for a in ("--own", name)]],
                instruments=ENGINE_INSTRUMENTS)
            sc = ["--base-url", self.base_url, "--k6-jsonl", k6jsonl,
                  *[a for p in blip_outs for a in ("--k6-jsonl", p)],
                  "--duration-s", duration + 5,
                  "--cache-probe-cmd",
                  f"{sys.executable} -c "
                  f"\"import sys;sys.path.insert(0,{str(self.root)!r});import os;"
                  f"import fae.experiment;fae.experiment._experiment.load(os.environ['EXPERIMENT_DIR']);"
                  f"from experiment.verifier import rig;"
                  f"sys.exit(0 if rig.cache_backend_ready(os.environ) else 1)\"",
                  "--ceiling-qps", ceiling_qps or 0,
                  "--sat-p99-ms", self.load_params()["sat_p99_ms"],
                  "--csv-output", trace_csv, "--events-output", trace_events]
            stream = str(self.cfg.get("STREAM_TRACE", "1")) == "1"
            if not stream:
                sc.append("--no-stream")
            # Its warnings go where they went before. Its live per-second table
            # is NOT captured when streaming: an operator watches that, and its
            # real outputs are files, so swallowing it would cost something and
            # buy nothing.
            blips.start()
            with (self.out / "sidecar.log").open("a") as slog, \
                    contextlib.redirect_stderr(slog):
                rc, _ = self.block("trace", sc,
                                         capture=not stream,
                                         out=slog if not stream else None)
            if rc:
                self.say("WARN: trace sidecar exited non-zero (see sidecar.log)")
            if k6.wait() != 0:
                self.say("WARN: k6 exited non-zero (see k6.log)")
            blips.join(30)
            sampler.join(30)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

        if not k6sum.exists():
            self.say("WARN[k6]: no summary export produced")
            r.stage_failed = "k6"
            return
        _, mout = self.block("k6", [k6sum, trace_csv, trace_events,
                                         self.out / "schedule.json"])
        r.load_total, r.load_errors, r.mount_delay_s = self.k6_counts(mout)
        self.say(f"k6: total={r.load_total} errors={r.load_errors}")
        self.say(f"provision latency: scaler decision -> cache ready = "
                 f"{r.mount_delay_s or 'n/a'}s")
        self.scaling_gate(trace_csv, trace_events)

    def scaling_gate(self, trace_csv, trace_events):
        if self.result.load_total is None:
            self.say("WARN[k6]: no load metrics — the scaling gate cannot judge")
            self.result.stage_failed = "k6"
            return
        """GREEN means the SCALING BEHAVIOUR matches the law, not merely that
        the e2e assertions pass: reject the blips, mount under sustained
        saturation, hold without flapping, release when traffic recedes."""
        r = self.result
        _, gout = self.block(
            "law",
            [trace_csv, trace_events, r.load_total, r.load_errors,
             self.out / "schedule.json"], env={"REPO_ROOT": self.root, **self.load_env()})
        ok, _, why = gout.partition("|")
        r.scaling_ok = (ok == "true")
        r.scaling_why = why
        if r.scaling_ok:
            self.say("scaling gate: OK (blips rejected, one mount per spike, "
                     "released when traffic receded)")
        elif ok == "void":
            # The ramp never took the store past its knee on this host: no
            # signal the policy could have acted on, so nothing to judge.
            self.say(f"FAIL[load-shape]: {why}")
            self._ledger("ALERT", f"LOAD-SHAPE {why}")
            r.stage_failed = "load-shape"
        else:
            self.say(f"FAIL[scaling]: {why}")
            self.say("  GREEN requires the scaling behavior to match the "
                     "specified law. Fix the provisioning policy.")
            r.stage_failed = "scaling"
            self.capture_cluster_diag()      # BEFORE teardown deletes it

    # --- the whole thing --------------------------------------------------

    def run(self):
        """Deploy, assert, load, judge, tear down. Returns the Result.

        EXIT RULE, unchanged: green iff e2e passed AND the scaling gate did not
        fail. `scaling_ok is None` means the load never ran (k6 absent), which
        is not a scaling failure.
        """
        t0 = time.time()
        r = self.result
        self.prepare_out()
        tech = self.tech()
        try:
            self.adopt_cell_env()

            self.say(f"db: bringing up capped Postgres "
                     f"(cpus={self.cfg.get('DB_CPUS')} mem={self.cfg.get('DB_MEM')})")
            ok, err = self.db_up()
            if not ok:
                self.say(f"FAIL[store]: capped store did not come up {err[-200:]}")
                r.stage_failed = "store"
                return r

            r.ceiling_qps = self.bench_ceiling()
            floor = int(self.cfg.get("CALIBRATION_MIN_QPS") or 40)
            rebenches = 0
            while r.ceiling_qps is None or r.ceiling_qps < floor:
                if r.ceiling_qps is None:
                    self.say("FAIL[store]: the store ceiling bench did not run")
                    r.stage_failed = "store"
                    return r
                if rebenches == self.MAX_REBENCHES:
                    self.say(f"FAIL[store]: ceiling still {r.ceiling_qps}qps < {floor} "
                             f"after {rebenches} re-benches")
                    r.stage_failed = "store"
                    return r
                # A collapsed ceiling means the HOST is overloaded, not the
                # store — the bench is CPU-bound. Any verdict judged against
                # that load shape is unfair to the build, so wait for a healthy
                # host rather than measure on a sick one. Bounded: the wait
                # holds the global verify lock, and a store that never comes
                # back would hold it forever.
                nap = 300 + int(time.time()) % 300
                self.say(f"HOST OVERLOADED: ceiling {r.ceiling_qps}qps < {floor} "
                         f"— sleeping {nap}s, then re-benching")
                self._ledger("ALERT", f"HOST-OVERLOADED ceiling={r.ceiling_qps}qps "
                                      f"< CALIBRATION_MIN_QPS={floor} — sleeping {nap}s")
                time.sleep(nap)
                rebenches += 1
                r.ceiling_qps = self.bench_ceiling()
            lp = self.load_params()
            self.say(f"load: baseline {lp['baseline']} -> spike ramps to {lp['top']} rps "
                     f"over {lp['ramp_s']}s, holds {lp['hold_s'] or '30/25'}s; blips ride "
                     f"the same ramp to {lp['blip_p99_ms']:g}ms or a queued request "
                     f"+{lp['blip_excess_req']} req over that rate; "
                     f"saturation = p99 >= {lp['sat_p99_ms']:g}ms, mount within "
                     f"{lp['max_mount_s']:g}s of it; store bench {r.ceiling_qps}qps "
                     f"(floor {floor})")

            if not rig.clean_start_wait(self.rigenv(), self.say):
                # A previous run's debris is a RIG condition, not a verdict, so
                # the attempt must not be consumed.
                self.say("FAIL[precheck]: a cache backend is deployed and could "
                         "not be cleaned — inspect the rig")
                r.stage_failed = "nostart"
                return r
            self.say("clean-start: no cache backend deployed (OK)")

            self.say(f"deploy: {tech} verify_setup (task={self.task} "
                     f"variant={self.variant_id} src={self.artifacts})")
            state = self.deploy(tech)
            if state == "missing":
                self.say(f"FAIL[deploy]: no variant named '{self.variant_id}' to bring up")
                r.stage_failed = "deploy"
                return r
            if state == "rejected":
                self.say("FAIL[deploy]: artifacts rejected after the infra "
                         "was up — see deploy.log")
                r.stage_failed = "deploy"
                return r
            if state == "nostart":
                self.say("FAIL[deploy]: infra never came up — see deploy.log")
                r.stage_failed = "nostart"
                return r
            if state == "contract":
                for x in self.pre_deploy:
                    self.say(f"FAIL[contract]: {x}")
                self.say("  the world before the bring-up is not the one the docs "
                         "describe — ATTEMPT VOID (rig fault, not a build verdict); "
                         "the cell stands down for the operator")
                r.stage_failed = "contract"
                r.contract = [str(x) for x in self.pre_deploy]
                return r
            r.deploy_ok = True

            broken = contract.for_variant(self).after_deploy()
            if broken:
                for x in broken:
                    self.say(f"FAIL[contract]: {x}")
                self.say("  the world after the bring-up is not the one the docs "
                         "describe — ATTEMPT VOID (rig fault, not a build verdict); "
                         "the cell stands down for the operator")
                r.stage_failed = "contract"
                r.contract = [str(x) for x in broken]
                return r

            held, want = self.readiness()
            if held < want:
                self.say(f"FAIL[nostart]: /health held only {held}/{want}s — "
                         f"the verify never started")
                r.stage_failed = "nostart"
                return r
            self._ledger("VERIFY_READY", f"held={held}s")
            self.say(f"readiness: held {held}/{want}s — verify started")
            if tech == "keda":
                self.keda_warmup()

            if not self.e2e():
                return r

            if rig.db_assert_caps(self.rigenv()) is not None:
                self.say("FAIL[precheck]: fixed store caps not enforced")
                r.stage_failed = "precheck"
                r.e2e_green = False
                return r
            _, body = self.http("GET", "/health", timeout=2)
            if re.search(r'"cache_mounted"\s*:\s*true', body):
                self.say("FAIL[precheck]: /health reports cache_mounted=true at "
                         "idle, right after deploy — the cache must START AT "
                         "ZERO and come up only under sustained load")
                r.stage_failed = "precheck"
                r.e2e_green = False
                return r

            self.run_load(r.ceiling_qps)
            return r
        finally:
            self.teardown(tech)
            self._gzip(self.out / "k6_results.jsonl")
            for f in sorted(self.out.glob("k6_blip_*.jsonl")):
                self._gzip(f)
            r.green = bool(r.e2e_green) and r.scaling_ok is not False
            r.seconds = time.time() - t0




class ElasticCacheVerifier(Verifier):
    """One arrangement: the Verify pipeline over `ctx`, its Result as a Verdict."""

    IMAGE_DIR = HERE            # the Dockerfile beside this module: the law's tools
    CPUS = 1                    # k6, the trace and the rig; the service has its own core
    EXCLUSIVE = None
    FILES = ARRANGEMENT_FILES
    ROOT_READS = ("tmp/cache",)       # keda's downloaded manifests
    REQUIRED_OUTPUTS = REQUIRED_OUTPUTS
    FEEDBACK_LOGS = ("verify.log", "deploy.log", "service.deploy.log",
                     "k6.log", "cluster-diag")
    MEASURED_STAGES = {"e2e", "k6", "scaling"}     # the load runs against the infra
    PREFIXES = {"container": rig.STORE_PREFIX, "network": rig.NET_PREFIX}
    RUN_LOG = service.SERVICE_LOG
    CALIBRATION_KEYS = tuple(k for k, (section, *_) in CONFIG.items() if section == "load")
    # A top below every host's knee: the spike never saturates the store.
    CALIBRATION_GUARD = {"variant": "pulumi_sealed_apidocs", "env": {"LOAD_TOP_RPS": 60},
                         "stage": "load-shape"}

    @classmethod
    def calibration_values(cls, arrangement_dir):
        """The contrib's per-spike values of one archived arrangement, read
        with the saturation p99 and the traffic-lag cap the law judges with
        (VERIFY_MAX_LAG_S, else the law's default)."""
        conf = fae.experiment.exp().config()
        lag = conf.get("VERIFY_MAX_LAG_S")
        return _calibration.values_from(
            arrangement_dir, sat_p99=load_params(conf.values)["sat_p99_ms"],
            **({"max_lag": float(lag)} if lag else {}))

    @classmethod
    def estimated_spread(cls, name, value):
        return _calibration.estimated_spread(name, value)

    @classmethod
    def identities(cls, cid):
        return [("container", f"{rig.STORE_PREFIX}{cid}"), ("network", f"{rig.NET_PREFIX}{cid}")]

    def teardown(self, ctx):
        """The store this verifier provisions, by its name: what a verify
        that did not finish leaves beside its container."""
        rig.db_down(rig.cell_identity(ctx.cid))
        VerifyDind.remove_for(ctx.cid)

    def verify(self, ctx):
        v = Verify(ctx.artifacts, ctx.out, task=ctx.task, variant=ctx.variant,
                   cid=ctx.cid, shape=ctx.arrangement, root=ctx.root, mode=ctx.mode, ctx=ctx)
        r = v.run()
        return Verdict(ok=r.green, stage=r.stage_failed, why=r.scaling_why or "",
                       charge=r.stage_failed not in VOID_STAGES, stand_down=tuple(r.contract),
                       metrics=r.to_metrics(), arrangement=v.arrangement_label(),
                       seconds=r.seconds)


def main(argv):
    """Artifact mode: one arrangement over an artifacts dir, no cell, no lock."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--task", default="T1")
    ap.add_argument("--variant", default="")
    ap.add_argument("--cid", default="")
    ap.add_argument("--shape", default=None)
    a = ap.parse_args(argv[1:])
    v = Verify(a.artifacts, a.out, task=a.task, variant=a.variant,
               cid=a.cid, shape=a.shape, mode="artifact")
    r = v.run()
    print(r)
    return 0 if r.green else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
