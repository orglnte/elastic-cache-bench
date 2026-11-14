"""The judged service in its own container, and what the three infra
classes share with the verifier that starts it.

The verifier runs inside `fae-verify-<cid>` (a container of the variant's
image, fae/cell/verify.py). Per arrangement it makes a fresh copy of the
artifacts, asks the variant's infra to bring its world up
(`ServiceInfra.verify_setup`, which answers an `Up`: the service's
environment, the networks it joins besides the cell's), then starts the
command of the variant's [verify.run] over the copy in the cell's secure
runner, `fae-secrun-<cid>` (fae/cell/infra/secrunner.py): the verify
image, one core, the cell's network and nothing of the rig — no repo, no
workspace, no Docker socket. It waits for the entrypoint contract (GET
/health on SERVICE_PORT answers 200) at the runner's name. On teardown it
stops the runner by that name, then the infra's `verify_teardown` runs.

`Rejected` is the one failure the verifier scores against the agent: the
infra was up and the artifacts did not run on it. Anything else a
bring-up raises is a rig fault, and the attempt is refunded.
"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from fae.cell.infra import secrunner
from fae.cell.infra.base import HookFailure, Infra

SERVICE_CPUS = 1
SERVICE_LOG = "service.deploy.log"
COPY = "run"                       # the service's copy of the artifacts, under the verify's out
SCRATCH = "secrun-scratch"         # its writable state: HOME, a Pulumi backend
# The verify's own environment the service is never given: the rig's paths
# and identity, which name nothing inside its container.
_RIG_ENV = ("REPO_ROOT", "EXPERIMENT_DIR", "PYTHONPATH", "HOME", "PATH", "USER", "PWD",
            "HOSTNAME", "KUBECONFIG", "WORKSPACES_DIR", "TRANSITIONS_LOG")


class Rejected(RuntimeError):
    """The artifacts were rejected after the infra came up: charged."""


@dataclass
class Up:
    """What an infra's bring-up hands the verifier: the service's own
    environment, the networks it joins besides the cell's (a cluster that
    scrapes it), and what the later stages must know (a kubeconfig)."""
    env: dict = field(default_factory=dict)
    networks: tuple = ()
    exported: dict = field(default_factory=dict)


class ServiceInfra(Infra):
    """The infra of a variant whose program is the judged service.
    `verify_setup` answers an `Up`; `TAG` names its lines in deploy.log and
    teardown.log."""
    TAG = ""

    def verify_setup(self, ctx, env):
        return Up()

    def not_ready(self, ctx, env):
        """What to record when the service it brought up never answered:
        the world as the bring-up left it, for the agent's feedback."""


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_http(url, timeout_s):
    for _ in range(int(timeout_s)):
        if http_ok(url):
            return True
        time.sleep(1)
    return False


def raise_fd_limit(want=65536):
    """The service holds a store pool, a cache pool and hundreds of inbound
    sockets under load; a low ceiling turns into EMFILE and a silently
    useless cache. Best effort: inherited by every child started after."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, hard), hard))
    except (ValueError, OSError):
        pass


def read_pid(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def stop_pid(path, wait_s=15, kill=True):
    """SIGTERM the process the pidfile names, wait for it to exit, SIGKILL
    it if it has not, and drop the file. Idempotent."""
    path = Path(path)
    pid = read_pid(path)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pid = None
    for _ in range(int(wait_s * 2)) if pid is not None else ():
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.5)
    else:
        if pid is not None and kill:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    path.unlink(missing_ok=True)


class Bringup:
    """One arrangement's bring-up: where it logs, the port it serves on, the
    service it starts."""

    def __init__(self, ctx, env, tag):
        self.ctx = ctx
        self.env = dict(env)
        self.tag = tag
        self.artifacts = Path(ctx.artifacts)
        self.out = Path(ctx.out)
        self.port = str(self.env.get("SERVICE_PORT") or "8080")
        self.host = secrunner.name_for(ctx.cid)

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    @property
    def copy(self):
        """The arrangement's copy of the artifacts: what the service runs and
        what a bring-up rewrites (never the judged tree)."""
        return self.out / COPY

    def fresh_copy(self):
        """Made by the verifier before the bring-up, once per arrangement."""
        return secrunner.fresh_copy(self.artifacts, self.out, COPY)

    def log(self, msg, name="deploy.log"):
        with (self.out / name).open("a") as f:
            f.write(f"[{self.tag}] {msg}\n")

    def clear_stale(self):
        """A runner left by a previous verify of this cell would answer
        /health for a stale build and the verify would grade that one."""
        secrunner.stop_by_name(self.ctx.cid, self.out)

    def runner(self, variant, up):
        """The variant's [verify.run] command over the copy, in the cell's
        secure runner on the cell's network and `up.networks`."""
        env = {k: v for k, v in self.env.items()
               if k not in _RIG_ENV and not k.startswith("FAE_")}
        env.update({k: str(v) for k, v in up.env.items()})
        env.update(PYTHONPATH="/workspace", TASK=self.ctx.task, SERVICE_PORT=self.port)
        try:
            return secrunner.for_variant(
                variant, self.ctx.cid, self.copy, env=env, networks=tuple(up.networks),
                scratch=self.out / SCRATCH, log=self.out / SERVICE_LOG, cpus=SERVICE_CPUS,
                cpuset=self.env.get("CPUSET_MEASURED") or os.environ.get("CPUSET_MEASURED") or None)
        except RuntimeError as e:
            raise HookFailure(f"the service runs only from inside the verify container: {e}")

    def start_service(self, variant, up):
        """The service over the copy; its output to service.deploy.log when
        it stops."""
        r = self.runner(variant, up)
        self.log(f"starting service in {r.name} on :{self.port} (task={self.ctx.task}, "
                 f"cpus={SERVICE_CPUS})")
        why = r.start()
        if why:
            raise HookFailure(f"service container did not start: {why}")
        return r

    def service_ready(self, timeout_s=60):
        """The entrypoint contract, held once; the verifier's readiness
        probe holds it for its window. Not ready is the agent's: Rejected."""
        if wait_http(f"{self.base_url}/health", timeout_s):
            self.log(f"ready: /health is 200 at {self.base_url}")
            return
        secrunner.SecRunner(image="", cid=self.ctx.cid, workdir=self.out, argv=(),
                            log=self.out / SERVICE_LOG).dump_logs()
        tail = ""
        try:
            tail = (self.out / SERVICE_LOG).read_text(errors="replace")[-3000:]
        except OSError:
            pass
        self.log(f"service not ready on :{self.port} within {timeout_s}s\n--- service.deploy.log (tail) ---\n{tail}")
        raise Rejected(f"service not ready on :{self.port} within {timeout_s}s")

    def stop_service(self):
        """Stop the service's runner by its name, keeping its output in
        service.deploy.log. Idempotent."""
        secrunner.stop_by_name(self.ctx.cid, self.out, log=self.out / SERVICE_LOG)


class Restarting:
    """A child kept running until stopped: started again a second after it
    exits (a port-forward to a Service that scales to zero drops and must
    reconnect when the backend is back)."""

    def __init__(self, argv, env, pidfile):
        self.argv, self.env, self.pidfile = list(argv), dict(env), Path(pidfile)
        self._stop = threading.Event()
        self._child = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._child = subprocess.Popen(self.argv, env=self.env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               start_new_session=True)
            except OSError:
                self._stop.wait(1)
                continue
            self.pidfile.write_text(f"{self._child.pid}\n")
            self._child.wait()
            self._stop.wait(1)

    def stop(self):
        self._stop.set()
        child = self._child
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass
        self._thread.join(5)
        self.pidfile.unlink(missing_ok=True)
