"""The keda variants' contract: what their seed docs promise about the
verification world, asked after the bring-up and after the teardown."""
from __future__ import annotations

import json
import os
import re

from fae.cell.infra.base import cksum
from experiment.verifier import contract as _base
from experiment.verifier import rig
from experiment.verifier.contract import Contract, Violation

DOCS = "experiment/variants/keda/seed"

_APP_HOST_RE = re.compile(r"(?:^|[^A-Za-z0-9_]|%2F)APP_HOST(?:[^A-Za-z0-9_]|$)",
                          re.MULTILINE)



class KedaContract(Contract):
    TECH = "keda"
    LAYOUT = f"{DOCS}/T*.keda*.project_layout.md"

    def ns(self):
        return self.env.get("NAMESPACE") or "ecb"

    def after_deploy(self):
        out = []
        # what the bring-up applied: the service's copy, rewritten
        from experiment.variants import service
        k8s = self.v.out / service.COPY / "k8s"
        left = [f.name for f in sorted(k8s.glob("*"))
                if f.is_file() and f.suffix in (".yaml", ".yml", ".json")
                and _APP_HOST_RE.search(f.read_text(errors="replace"))]
        if left:
            out.append(Violation(f"{self.LAYOUT}: the bring-up rewrites every `APP_HOST`",
                                 "a manifest still names APP_HOST after the deploy",
                                 " ".join(left)))
        ns = self.ns()
        if not self.ok(["kubectl", "get", "svc", "redis", "-n", ns]):
            out.append(Violation(f"{self.LAYOUT}: the Redis Service must be named `redis`",
                                 f"no Service `redis` in ns/{ns} after the apply"))
        elif not rig._k8s_cache_deployments(ns, self.env):
            out.append(Violation(f"{self.LAYOUT}: a Redis Deployment scaled 0<->1 behind Service `redis`",
                                 f"Service `redis` selects no Deployment in ns/{ns}"))
        for f in sorted(k8s.glob("*")):
            if f.is_file() and f.suffix in (".yaml", ".yml", ".json") and \
                    not self.ok(["kubectl", "get", "-f", str(f), "-n", ns]):
                out.append(Violation(f"{self.LAYOUT}: deploy applies every `k8s/*.yaml`",
                                     "an authored manifest is not in the cluster after the apply",
                                     f.name))
        rc, avail, _ = self.run(["kubectl", "-n", "keda", "get", "deploy", "keda-operator",
                                 "-o", "jsonpath={.status.availableReplicas}"])
        if rc != 0 or not avail.strip() or int(avail.strip() or 0) < 1:
            out.append(Violation(f"{self.LAYOUT}: **KEDA** already installed",
                                 "keda-operator has no available replica"))
        if not self.ok(["kubectl", "-n", "kube-system", "get", "deploy", "metrics-server"]):
            out.append(Violation(f"{self.LAYOUT}: **metrics-server** already installed",
                                 "no metrics-server Deployment in kube-system"))
        return out

    def after_teardown(self):
        out = []
        name = f"ecb-ka-{cksum(f'{self.v.cid}-verify')}"
        rc, clusters, _ = self.run(["kind", "get", "clusters"])
        if rc == 0 and name in clusters.split():
            out.append(Violation(f"{self.LAYOUT}: every verification starts from a clean world",
                                 "the verify cluster survived the teardown", name))
        if (self.v.out / ".kubecfg").exists():
            out.append(Violation(f"{self.LAYOUT}: every verification starts from a clean world",
                                 "the verify cluster's kubeconfig survived the teardown"))
        return out


CONTRACT = KedaContract
