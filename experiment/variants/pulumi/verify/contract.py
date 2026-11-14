"""The pulumi variants' contract: what their seed docs promise about the
verification world, asked after the bring-up and after the teardown."""
from __future__ import annotations

import json
import os
import re

from fae.cell.infra.base import cksum
from experiment.verifier import contract as _base
from experiment.verifier import rig
from experiment.verifier.contract import Contract, Violation

DOCS = "experiment/variants/pulumi/seed"


class PulumiContract(Contract):
    TECH = "pulumi"
    LAYOUT = f"{DOCS}/T*.pulumi*.project_layout.md"

    def docker(self, *args):
        return ["docker", "-H", self.env["PULUMI_DOCKER_HOST"], *args]

    def before_deploy(self):
        # Measured before the service runs: once it starts it may legitimately
        # select or preview its stack, which writes into this backend.
        from experiment.variants import service
        state = self.v.out / service.SCRATCH / ".pulumi-state"
        stale = [p.name for p in state.glob("**/*") if p.is_file()] if state.exists() else []
        if stale:
            return [Violation(f"{self.LAYOUT}: the Pulumi state backend is emptied after each verification",
                              "the state backend holds files at bring-up", " ".join(stale[:5]))]
        return []

    def after_deploy(self):
        out = []
        if not self.env.get("PULUMI_DOCKER_HOST"):
            out.append(Violation(f"{self.LAYOUT}: `PULUMI_DOCKER_HOST` set for the service process",
                                 "PULUMI_DOCKER_HOST is not in the verify's env"))
        elif not self.ok(self.docker("info")):
            out.append(Violation(f"{self.LAYOUT}: `PULUMI_DOCKER_HOST` set for the service process",
                                 "the provisioning docker host does not answer",
                                 self.env["PULUMI_DOCKER_HOST"]))
        if not (self.env.get("PULUMI_CACHE_HOST") and self.env.get("PULUMI_CACHE_PORT")):
            out.append(Violation(f"{self.LAYOUT}: `PULUMI_CACHE_HOST`, `PULUMI_CACHE_PORT` set",
                                 "the cache endpoint is not in the verify's env"))
        out += _base.port_owner_violation(self)
        return out

    def after_teardown(self):
        out = []
        out += _base.service_gone_violation(self, self.LAYOUT)
        if not self.env.get("PULUMI_DOCKER_HOST"):
            out.append(Violation(f"{self.LAYOUT}: `PULUMI_DOCKER_HOST` set for the service process",
                                 "PULUMI_DOCKER_HOST is not in the verify's env"))
            return out
        rc, ps, _ = self.run(self.docker("ps", "-q"))
        if rc == 0 and ps.split():
            out.append(Violation(f"{self.LAYOUT}: teardown removes the provisioning substrate wholesale",
                                 "containers remain in the provisioning docker host",
                                 f"{len(ps.split())} container(s)"))
        return out


CONTRACT = PulumiContract
