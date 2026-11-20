"""The pulumi overlay: rides on FAE's DockerSubstrate (`lifecycle = "split"`).
Records pulumi.env for the agent sandbox when access is enabled, wires
Pulumi's local state backend and cache env vars during verify, and resets
inner containers on verify teardown."""
from __future__ import annotations

from pathlib import Path

from fae.cell.substrate import DomainOverlay

from .. import service

HERE = Path(__file__).resolve().parent


class PulumiOverlay(DomainOverlay):
    TAG = "pulumi"
    ENV_FILE = "pulumi.env"
    VERIFY_ADOPTS = ()

    @staticmethod
    def verify_endpoints(substrate):
        return {
            "PULUMI_DOCKER_HOST": substrate.docker_host,
            "PULUMI_CACHE_HOST": substrate.host,
            "PULUMI_CACHE_PORT": "6379",
        }

    def on_cell_setup(self, substrate, infra):
        if substrate is None:
            return None
        infra.record_env([
            ("PULUMI_DOCKER_HOST", f"tcp://{substrate.host}:2375"),
            ("PULUMI_CACHE_HOST", substrate.host),
            ("PULUMI_CACHE_PORT", "6379"),
        ])
        infra.log(f"pulumi: sidecar ready; endpoints recorded in {infra.env_file}")
        return None

    def on_verify_setup(self, substrate, ctx, env):
        b = service.Bringup(ctx, env, f"deploy-{self.TAG}")
        own = self.verify_endpoints(substrate)
        (b.out / service.SCRATCH / ".pulumi-state").mkdir(parents=True, exist_ok=True)
        # the infra is up; from here every failure is the agent's
        b.log(f"starting service on :{b.port} (task={ctx.task}, infra={substrate.docker_host})")
        passphrase = (ctx.conf.get("PULUMI_CONFIG_PASSPHRASE") if ctx.conf else None) or "ecb-local"
        return service.Up(
            env={
                "PULUMI_BACKEND_URL": "file:///scratch/.pulumi-state",
                "PULUMI_CONFIG_PASSPHRASE": passphrase,
                "DOCKER_HOST": substrate.docker_host,
                **own,
                "CACHE_HOST": own["PULUMI_CACHE_HOST"],
                "CACHE_PORT": own["PULUMI_CACHE_PORT"],
            },
            exported=own,
        )

    def on_verify_teardown(self, substrate, ctx, env):
        b = service.Bringup(ctx, env, f"teardown-{self.TAG}")
        if substrate is not None:
            substrate.reset()
        b.log("torn down", "teardown.log")
