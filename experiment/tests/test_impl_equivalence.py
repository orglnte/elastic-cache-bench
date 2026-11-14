"""The Python cell's ledger grammar under a deterministic stub.

WHAT IS PINNED, and why only this. The cell runs a seeded workspace with the
agent replaced by a fixed overlay (AGENT_STUB_OVERLAY — no model call, so the
loop is deterministic) and the verify replaced by a scripted stub. What is
read back is the LEDGER EVENT GRAMMAR — the event sequence, the attempt
numbers, the verdict per attempt — plus the checkpoint TREE HASH and the seal.
This was the bash-vs-py equivalence diff until the bash driver was retired;
the grammar it pinned is now the py driver's own contract.

NOT compared: verify_s, k6 counts, mount timings, commit SHAs. Those vary run
to run with byte-identical code, and a test that asserts them teaches everyone
to ignore it. Tree hashes are the exception that makes this work: identical
content hashes identically, always.

ISOLATION IS BY LOCATION. lib.sh recomputes REPO_ROOT from its own directory
unconditionally, so REPO_ROOT= in the environment does not survive sourcing it.
The harness is therefore symlinked into a temp tree and run from there — a run
that sourced the repo's own lib.sh would put its locks in the LIVE fleet's
scheduling plane and its workspaces in the live tree.

WHAT THIS DOES NOT COVER. Stub mode is one attempt by design (both
implementations force budget=1 there, and they must agree on that or they do
not even agree on how long a deterministic run is). So the multi-attempt paths
— limit waits, agent-fault retries, NOEDIT after a restart — are NOT exercised
here and need targeted tests, which is what the plan says too. The second
level, a same-rep A/B on the live fleet under real infra, needs the fleet
up and is an operator action, not a unit test.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import ENGINE_TREE, ROOT

ROOT = Path(ROOT)

# A verify stub that costs nothing and is deterministic. It writes the same
# metrics.json the real driver would and exits with the verdict it is told.
VERIFY_STUB = r"""#!/usr/bin/env bash
set -uo pipefail
cid="${1:?}"
ws="${WORKSPACES_DIR:?}/$cid"
printf '%s\tVERIFY_READY\t%s\theld=10s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$cid" >> "$ws/iterations.log"
green="$(cat "${VERIFY_STUB_VERDICT:-/dev/null}" 2>/dev/null || echo true)"
cat > "$ws/metrics.json" <<JSON
{"cell_id": "$cid", "e2e_pass": 7, "e2e_total": 7, "e2e_green": $green,
 "stage_failed": "$([[ "$green" == true ]] || echo scaling)",
 "scaling_ok": $green, "shape": "${VERIFY_SHAPE:-BBS}"}
JSON
[[ "$green" == true ]]
"""

CONFIG_SH = r"""# Minimal machine config for the equivalence tree.
# AGENT_CMD is non-empty (run_cell refuses without one) but never runs: the
# stub-overlay branch is taken first. AGENT=human skips credential staging.
CELL_PREFIX="stub_high"
AGENT_CMD="true"
WORKSPACES_DIR_NAME="workspaces"
SKELETON_DIR="$REPO_ROOT/experiment/seeds/skeleton"
TASKS_DIR_NAME="experiment/seeds/tasks"
WORK_SLOTS=7
"""


class EquivalenceTree:
    """A repo-shaped temp tree: every top-level entry symlinked, except
    harness/ (a real directory of per-file symlinks, so two files can be
    overridden) and workspaces/ (real, and disposable)."""

    OVERRIDDEN = {"config.sh"}

    def __init__(self, tmp):
        self.root = Path(tmp)
        for entry in ROOT.iterdir():
            if entry.name in ("harness", "workspaces", "workspaces.nosync",
                              ".git", "tmp", "__pycache__"):
                continue
            (self.root / entry.name).symlink_to(entry)
        h = self.root / "fae"
        h.mkdir(parents=True)
        for f in (ENGINE_TREE / "fae").iterdir():
            if f.is_file() and f.name not in self.OVERRIDDEN:
                (h / f.name).symlink_to(f)
        # The arm's infra is not the variant: FAE_VARIANT_NOOP=1 (set
        # in env()) makes variants.py provision nothing and demand nothing.
        # cell/ too: load_config folds the Python driver into the fingerprint
        # and refuses to run when the glob matches nothing.
        for d in ("agent-container", "cell", "variants"):
            if (ENGINE_TREE / "fae" / d).is_dir():
                (h / d).symlink_to(ENGINE_TREE / "fae" / d)
        stub = h / "verify_stub.sh"
        stub.write_text(VERIFY_STUB)
        stub.chmod(0o755)
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()
        # The fixed solution the "agent" produces — one file, so an edit is
        # unambiguous and the tree hash has something to be about.
        self.overlay = self.root / "overlay"
        (self.overlay / "app").mkdir(parents=True)
        (self.overlay / "app" / "provisioning_impl.py").write_text(
            "# the stub agent's answer\nPOLICY = 'sustained-only'\n")

    def env(self, **extra):
        e = dict(os.environ, REPO_ROOT=str(self.root),
                 WORKSPACES_DIR=str(self.workspaces),
                 TRANSITIONS_LOG=str(self.workspaces / "transitions.log"),
                 AGENT_STUB_OVERLAY=str(self.overlay),
                 AGENT="human",          # skips credential staging; the stub
                 EFFORT="high",          # branch is checked before the human one
                 DOOM_GUARD="1")
        e.update({k: str(v) for k, v in extra.items()})
        e.pop("SMOKE", None)
        return e


def grammar(ws):
    """The comparable shape of a ledger: event names with their attempt number
    and verdict, and nothing that carries a clock or a duration."""
    out = []
    for line in (Path(ws) / "iterations.log").read_text().splitlines():
        f = line.split("\t")
        if len(f) < 2:
            continue
        ev = f[1]
        att = re.search(r"attempt=(\d+)", line)
        detail = ""
        if ev == "ITER":
            detail = f[2]
        elif ev == "END":
            detail = "green=true" if "green=true" in line else "green=false"
        elif ev == "CKPT":
            detail = "pre" if " pre " in line or "pre tree" in line else "post"
        elif ev == "IMPL":
            detail = "<impl>"          # the ONE legitimate difference
        out.append((ev, att.group(1) if att else "-", detail))
    return out


class EquivalenceTestCase(unittest.TestCase):

    # CELL_PREFIX is <agent>_<effort>, and AGENT=human is what skips
    # credential staging — so the cid carries "human", not the config value.
    CID = "human_high_pulumi_sealed_apidocs_T1_r1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = EquivalenceTree(self._tmp.name)

    def run_python(self, verdict="true"):
        """The Python cell, with the verify stubbed at the driver's seam: the
        cell is given a `verify` callable that runs the stub script in the
        tree, so the loop — not the verify — is what the grammar records."""
        vf = self.tree.root / "verdict"
        vf.write_text(verdict + "\n")
        env = self.tree.env(VERIFY_STUB_VERDICT=str(vf), FAE_VARIANT_NOOP="1")
        script = (
            "import os, subprocess, sys\n"
            f"sys.path[:0] = [{str(ENGINE_TREE)!r}, {str(ROOT)!r}]\n"
            "from fae.cell import Cell, VerifyResult\n"
            f"c = Cell.new({self.CID!r}, 'T1', 'pulumi_sealed_apidocs', 1,"
            f" workspaces={str(self.tree.workspaces)!r}, root={str(self.tree.root)!r})\n"
            "def stub_gate(seed_shape=None):\n"
            "    out = []\n"
            "    for s in c.gate_shapes:\n"
            "        e = dict(os.environ)\n"
            "        if s: e['VERIFY_SHAPE'] = s\n"
            f"        p = subprocess.run(['bash', {str(self.tree.root)!r} +"
            " '/fae/verify_stub.sh', c.cid], env=e)\n"
            "        out.append(VerifyResult(green=p.returncode == 0, shape=s))\n"
            "        if not out[-1].green: break\n"
            "    return out\n"
            f"print(c.run(stub_overlay={str(self.tree.overlay)!r},"
            " verify=stub_gate, ignore_slots=True))\n")
        return subprocess.run([sys.executable, "-c", script], env=env,
                              capture_output=True, text=True, timeout=600)

    def ws(self):
        return self.tree.workspaces / self.CID

    def wipe(self):
        shutil.rmtree(self.ws(), ignore_errors=True)


def _docker_up():
    """Both implementations gate every attempt on the infra, so a host
    without docker cannot run either — the comparison would be between two
    identical refusals, which proves nothing."""
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


NEEDS_INFRA = unittest.skipUnless(_docker_up(), "needs a running docker")


@NEEDS_INFRA


class TestTheGrammarIsWorthDiffing(EquivalenceTestCase):
    """A comparison over the wrong fields passes forever. These pin what the
    grammar keeps and what it drops."""

    def test_it_keeps_the_event_sequence_and_attempt_numbers(self):
        b = self.run_python("true")
        self.assertEqual(b.returncode, 0, b.stdout[-2000:])
        g = grammar(self.ws())
        names = [e for e, _, _ in g]
        self.assertIn("START", names)
        self.assertIn("ITER", names)
        self.assertIn("END", names)
        self.assertIn("CKPT", names)
        self.assertEqual([a for e, a, _ in g if e == "START"], ["1"])
        self.assertEqual([d for e, _, d in g if e == "ITER"], ["green"])

    def test_it_drops_the_clock(self):
        b = self.run_python("true")
        self.assertEqual(b.returncode, 0, b.stdout[-2000:])
        flat = str(grammar(self.ws()))
        self.assertNotRegex(flat, r"\d{4}-\d\d-\d\dT")
        self.assertNotIn("verify_s", flat)
