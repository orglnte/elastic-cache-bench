"""The rig's contract with the docs handed to the agent: the base, the shared
checks and the hook the verifier calls.

Each check states one promise a seed doc makes about the verification world
(`doc_ref`) and asks the world whether it holds — after the bring-up, before
the readiness hold; and after the teardown. A violation is a RIG fault: the
arrangement is void (never charged), the ledger carries an ALERT, and the cell
stands down for the operator. Nothing here repairs anything.

Checks read files and run kubectl/docker through the verify's own env, so
they ask the cluster and the sidecar the verify used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import rig
import fae.experiment


@dataclass
class Violation:
    doc_ref: str      # the seed doc line the check enforces
    what: str         # what the world looked like instead
    evidence: str = ""

    def __str__(self):
        e = f" [{self.evidence}]" if self.evidence else ""
        return f"{self.doc_ref} — {self.what}{e}"


class Contract:
    """One variant's promises. Subclasses list checks; this class runs them."""

    TECH = ""

    def __init__(self, verify):
        self.v = verify
        self.env = verify.rigenv()

    def run(self, argv, timeout=20):
        try:
            return rig._run(argv, timeout=timeout, env=self.env)
        except (OSError, rig.subprocess.TimeoutExpired) as e:
            return 1, "", str(e)

    def ok(self, argv, timeout=20):
        return self.run(argv, timeout)[0] == 0

    def before_deploy(self):
        """Checks on the world the bring-up is about to start in."""
        return []

    def after_deploy(self):
        return []

    def after_teardown(self):
        return []


# --- shared checks -------------------------------------------------------------

def read_pid(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def port_owner_violation(c):
    """The service on the app port must be the one the bring-up started. It
    runs in the cell's own runner container, which the bring-up replaces, so
    the port is that container's by construction: nothing to compare here;
    readiness decides whether it serves."""
    return []


def service_gone_violation(c, layout):
    """The teardown removes the cell's runner; one that survives would serve
    the next verify a stale build."""
    from fae.cell.infra import secrunner
    name = secrunner.name_for(c.v.cid)
    rc, out, _ = c.run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"])
    if rc == 0 and out.strip():
        return [Violation(f"{layout}: teardown stops the service",
                          "the service's container is still there after the teardown", name)]
    return []


def for_variant(verify):
    """The variant's contract (its verify/contract.py, CONTRACT), else the
    empty base — artifact mode names no variant."""
    import importlib
    cls = fae.experiment.exp().definition.variant(verify.variant_id) if verify.variant_id else None
    if cls is None:
        return Contract(verify)
    try:
        mod = importlib.import_module(cls.INFRA.__module__ + ".verify.contract")
    except ModuleNotFoundError:
        return Contract(verify)
    return getattr(mod, "CONTRACT", Contract)(verify)
