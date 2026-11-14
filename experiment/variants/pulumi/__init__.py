"""The pulumi infra: a dind sidecar on a disjoint port base, on the cell
network, the daemon the agent's program provisions into; the agent reaches
it too when its variant has access. After each arrangement the sidecar's
inner world is cleared."""
from __future__ import annotations

import os
from pathlib import Path

from fae.cell.infra import base
from fae.cell.infra.base import HookFailure, host_ports
from fae.cell.infra.dind import DindSidecar, VerifyDind

from .. import service

HERE = Path(__file__).resolve().parent


class Pulumi(service.ServiceInfra):
    TAG = "pulumi"
    ENV_FILE = "pulumi.env"
    PREFIXES = {"container": (DindSidecar.PREFIX, VerifyDind.PREFIX)}
    # the verify starts its own daemon (VerifyDind) and adopts nothing of the cell's
    VERIFY_ADOPTS = ()
    # ports on the host's loopback; a cell takes one slot of each by the hash of its id
    HOST_PORT_RANGES = {"api": range(23800, 24600),    # the sidecar's docker API
                        "cache": range(24600, 25400)}  # the cache it publishes

    @classmethod
    def identities(cls, cid):
        return [("container", DindSidecar.name_for(cid)), ("container", VerifyDind.name_for(cid))]

    def shared_containers(self):
        """None: the sidecar is the agent's alone; the verify runs its own."""
        return []

    @classmethod
    def verify_endpoints(cls, cid):
        """The verify's own daemon and the cache it publishes, by name on
        the cell network of the verify host."""
        return {"PULUMI_DOCKER_HOST": VerifyDind.url_for(cid),
                "PULUMI_CACHE_HOST": VerifyDind.name_for(cid), "PULUMI_CACHE_PORT": "6379"}

    def ports(self):
        return host_ports(self.cid, self.HOST_PORT_RANGES)

    def sidecar(self, api_port, cache_port):
        return DindSidecar(self.cid, api_port, cache_port, 6379,
                           self.cfg("DIND_IMAGE", "docker:27-dind"),
                           self.cfg("CACHE_IMAGE", "redis:7-alpine"),
                           self.log, "pulumi", network=self.network())

    @classmethod
    def cell_env(cls, cid):
        """What the verify reads from pulumi.env: the sidecar's daemon and
        the cache it publishes, by the sidecar's name on the cell network."""
        name = DindSidecar.name_for(cid)
        return [("PULUMI_DOCKER_HOST", f"tcp://{name}:2375"),
                ("PULUMI_CACHE_HOST", name), ("PULUMI_CACHE_PORT", "6379")]

    def alive(self):
        p = self.ports()
        api, cache = p["api"], p["cache"]
        return self.sidecar(api, cache).answers()

    def cell_setup(self):
        """The sidecar; with access, the agent's DOCKER_HOST is its daemon."""
        p = self.ports()
        api, cache = p["api"], p["cache"]
        self.sidecar(api, cache).ensure()
        self.record_env(self.cell_env(self.cid))
        self.log(f"pulumi: sidecar ready; endpoints recorded in {self.env_file}")
        if not self.variant.ACCESS_INFRA:
            return {}
        return {"KUBE_MOUNT": f"-e DOCKER_HOST=tcp://host.docker.internal:{api}"}

    def ok(self):
        """The host carries the sidecar; the verify's tools are the image's."""
        dind = self.cfg("DIND_IMAGE", "docker:27-dind")
        cache = self.cfg("CACHE_IMAGE", "redis:7-alpine")
        if not base._ok(["docker", "info"]):
            self.log("HALT[infra]: docker unreachable")
            return False
        for img in (dind, cache):
            if not base._ok(["docker", "image", "inspect", img]):
                self.log(f"HALT[infra]: image '{img}' not present — docker pull {img}")
                return False
        return True

    def cell_teardown(self):
        p = self.ports()
        api, cache = p["api"], p["cache"]
        self.sidecar(api, cache).remove()

    # --- the verify side: one arrangement, inside the verify container ---

    def verify_setup(self, ctx, env):
        """The service alone: its own authored code provisions the cache at
        runtime through the Pulumi automation API, against a daemon of this
        verify's own (VerifyDind), started fresh here. No control plane runs
        — the arm is deliberately ungoverned."""
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        host = VerifyDind(ctx.cid, self.cfg("DIND_IMAGE", "docker:27-dind"),
                          self.cfg("CACHE_IMAGE", "redis:7-alpine"),
                          env.get("FAE_CELL_NET") or os.environ.get("FAE_CELL_NET", ""),
                          log=lambda m: b.log(m)).start()
        own = self.verify_endpoints(ctx.cid)
        (b.out / service.SCRATCH / ".pulumi-state").mkdir(parents=True, exist_ok=True)
        # the infra is up; from here every failure is the agent's
        b.log(f"starting service on :{b.port} (task={ctx.task}, infra={host})")
        return service.Up(env={
            "PULUMI_BACKEND_URL": "file:///scratch/.pulumi-state",
            "PULUMI_CONFIG_PASSPHRASE": self.cfg("PULUMI_CONFIG_PASSPHRASE", "ecb-local"),
            "DOCKER_HOST": host, "PULUMI_DOCKER_HOST": host,
            **{k: own[k] for k in ("PULUMI_CACHE_HOST", "PULUMI_CACHE_PORT")},
            "CACHE_HOST": own["PULUMI_CACHE_HOST"], "CACHE_PORT": own["PULUMI_CACHE_PORT"]})

    def verify_teardown(self, ctx, env):
        """Clear every container inside the sidecar, once the service is
        stopped (its shutdown hook may be driving a destroy() through the
        engine): the inner world is verify scratch, and the agent's leftovers
        would read as residue at the next clean-start check."""
        b = service.Bringup(ctx, env, f"teardown-{self.TAG}")
        host = env.get("PULUMI_DOCKER_HOST") or VerifyDind.url_for(ctx.cid)
        inner = base._run(["docker", "-H", host, "ps", "-aq"]).stdout.split()
        if inner:
            base._run(["docker", "-H", host, "rm", "-f", *inner])
            b.log(f"cleared inner container(s): {' '.join(inner)}", "teardown.log")
        b.log("torn down", "teardown.log")

    def verify_down(self, ctx):
        """The verify's own daemon, once its teardown and contract are done."""
        VerifyDind.remove_for(ctx.cid)
