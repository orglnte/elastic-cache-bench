"""The harness/agent contract, and the attempt loop's state transitions.

AGENT=testagent is a real agent: same spawn, same container, same two mounts as
any other. It audits everything it was handed into findings.json and then acts
to TESTAGENT_PLAN, so the loop becomes reproducible — fail twice then solve,
edit nothing, touch a fixed file, die silently — without tokens or model
variance.

The AGENT runs for real. The VERIFY is scripted, because the variant here is
the contract and the transitions; tests/test_verify_port.py owns the verify's
own rules, and a real six-arrangement gate is a quarter-hour of exclusive rig
per attempt.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import ENGINE_TREE, ROOT

ROOT = Path(ROOT)

# Green unless the agent left its marker. Records each call so a test can prove
# the verify was SKIPPED for a no-edit attempt.
# The cell is driven through a script rather than `-m fae.cell` so the GATE
# can be passed in: the cell calls Verify in-process, so there is no verifier
# script to replace. Everything else is real — prepare,
# locks, the treatment hook, the container, the ledger, checkpoints, the seal.
DRIVER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO_ROOT"])
from fae.cell import Cell, VerifyResult

calls = os.environ["VERIFY_CALLS"]
c = Cell(os.environ["CID"], workspaces=os.environ["WORKSPACES_DIR"],
         root=os.environ["REPO_ROOT"])
c._env.update(TASK="T1", VARIANT=os.environ["VARIANT"], REPEAT=os.environ["REP"])

def gate(attempt=1, seed_shape=None):
    """Green unless the agent left its unsolved marker."""
    with open(calls, "a") as f:
        f.write("x\n")
    ref = Path(os.environ["REPO_ROOT"]) / "experiment/variants/pulumi/seed/reference/overlay"
    solved = all((c.artifacts / f.relative_to(ref)).exists()
                 for f in ref.rglob("*") if f.is_file())
    broken = not solved or (c.artifacts / "app" / "testagent_stub.py").exists()
    (c.ws / "metrics.json").write_text(json.dumps(
        {"e2e_pass": 7, "e2e_total": 7, "stage_failed": "" if not broken else "scaling"}))
    with (c.ws / "verify.log").open("a") as f:
        f.write(f"verify: green={not broken}\n")
    c._append("VERIFY_READY", "held=10s")
    r = VerifyResult(green=not broken, shape=None,
                     stage_failed="" if not broken else "scaling")
    if r.green:
        c._append("SHAPE", f"attempt={attempt}", "seed pass")
    else:
        c._append("SHAPE", f"attempt={attempt}", "seed FAIL (passed: none)")
    return [r]

from fae.cell import Halt, Sealed
try:
    print(c.run(ignore_slots=True, verify=gate))
except Sealed as e:
    print(f"refusing: {e}", file=sys.stderr)
    sys.exit(46)
except Halt as e:
    print(str(e), file=sys.stderr)
    sys.exit(e.code)
'''

# The scripted testagent runs in its container from the staged .agent-testagent
# home; the driver reads AGENT_CMD from the environment as an override.
STUB_AGENT_CMD = (
    'docker run --rm --name "fae-agent-$CELL_ID" '
    '-v "$art":/workspace -w /workspace '
    '-v "$AGENT_TESTAGENT":/home/node/.testagent '
    '-e CELL_ID -e VARIANT -e TESTAGENT_PLAN -e SERVICE_PORT '
    '$AGENT_IMAGE python3 /home/node/.testagent/testagent.py "$(cat "$PROMPT_FILE")"')


def _docker_ok():
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _image_ok():
    try:
        return subprocess.run(["docker", "image", "inspect", "fae-ecb-variants-pulumi-agent:latest"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


NEEDS_AGENT = unittest.skipUnless(
    _docker_ok() and _image_ok(), "needs docker and the agent image")


@NEEDS_AGENT
class ContractTestCase(unittest.TestCase):
    """A repo-shaped tree with only the verify scripted."""

    # A rep the fleet does not use: the container name is global,
    # so a real testagent cell and this suite would collide on it.
    CID = "testagent_high_pulumi_sealed_apidocs_T1_r90"
    VARIANT = "pulumi_sealed_apidocs"

    PLAN = None
    TIMEOUT = 600
    RETRY_S = 1
    FAULT_RETRIES = 2

    @classmethod
    def setUpClass(cls):
        if cls.PLAN is None:
            raise unittest.SkipTest("base class")
        cls._build()
        cls.p = cls.run_cell(cls.PLAN, cls.TIMEOUT)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", f"fae-agent-{cls.CID}"],
                       capture_output=True)
        if getattr(cls, "_tmp", None):
            cls._tmp.cleanup()

    @classmethod
    def _build(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        self = cls
        self.root = Path(self._tmp.name)
        for entry in ROOT.iterdir():
            if entry.name in ("harness", "workspaces", "workspaces.nosync",
                              ".git", "tmp", "__pycache__"):
                continue
            (self.root / entry.name).symlink_to(entry)
        h = self.root / "fae"
        h.mkdir(parents=True)
        for f in (ENGINE_TREE / "fae").iterdir():
            if f.is_file() and f.name != "config.sh":
                (h / f.name).symlink_to(f)
        for d in ("agent-container", "cell", "conduct", "experiment", "variants"):
            if (ENGINE_TREE / "fae" / d).is_dir():
                (h / d).symlink_to(ENGINE_TREE / "fae" / d)
        # Provisioning is not the variant here (a dind sidecar per test would
        # dominate the runtime): the arm runs under FAE_VARIANT_NOOP.

        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()
        self.ws = self.workspaces / self.CID
        self.calls = self.root / "verify_calls"
        self.calls.write_text("")

    @classmethod
    def run_cell(cls, plan, timeout=600):
        self = cls
        driver = self.root / "drive.py"
        driver.write_text(DRIVER)
        env = dict(os.environ, REPO_ROOT=str(self.root),
                   WORKSPACES_DIR=str(self.workspaces),
                   TRANSITIONS_LOG=str(self.workspaces / "transitions.log"),
                   AGENT="testagent", EFFORT="high", CONDITION="apidocs",
                   AGENT_IMAGE="fae-ecb-variants-pulumi-agent:latest",
                   AGENT_CMD=STUB_AGENT_CMD, SERVICE_PORT="8080", WORK_SLOTS="7",
                   FAE_VARIANT_NOOP="1",
                   TESTAGENT_PLAN=plan, VERIFY_CALLS=str(self.calls),
                   CID=self.CID, VARIANT=self.VARIANT, REP="90",
                   HB_TICK="3600",
                   # The retry PATH is the variant; its production wait (600s)
                   # is not, and a suite that sleeps it would never be run.
                   LIMIT_RETRY_S=str(self.RETRY_S),
                   AGENT_FAULT_RETRIES=str(self.FAULT_RETRIES))
        # one arrangement: the injected gate returns one result (a smoke-only cut)
        env.update(SHAPE_GATE="one", SMOKE="1")
        return subprocess.run([sys.executable, str(driver)], cwd=str(self.root),
                              env=env, capture_output=True, text=True,
                              timeout=timeout)

    # --- reading what happened -------------------------------------------

    def findings(self):
        f = self.ws / ".agent-testagent" / "findings.json"
        return json.loads(f.read_text()) if f.is_file() else []

    def events(self):
        return [l.split("\t")[1]
                for l in (self.ws / "iterations.log").read_text().splitlines()
                if len(l.split("\t")) > 1]

    def iters(self):
        return [l.split("\t")[2]
                for l in (self.ws / "iterations.log").read_text().splitlines()
                if "\tITER\t" in l]

    def transitions(self):
        f = self.workspaces / "transitions.log"
        return [l.split("\t")[1] for l in f.read_text().splitlines()] \
            if f.is_file() else []

    def waits(self):
        return [l.split("\t", 3)[3]
                for l in (self.ws / "iterations.log").read_text().splitlines()
                if "\tWAIT\t" in l]

    def verify_calls(self):
        return len(self.calls.read_text().split())


class TestWhatTheAgentIsHanded(ContractTestCase):
    """Every field here is something the harness can get wrong in a way that
    costs an attempt and reads as an agent failure."""

    PLAN = "green"

    @property
    def f(self):
        found = self.findings()
        self.assertTrue(found, f"the agent never ran:\n{self.p.stderr[-2000:]}")
        return found[0]

    def test_it_works_in_the_mounted_artifacts_dir(self):
        self.assertEqual(self.f["cwd"], "/workspace")

    def test_it_receives_the_prompt_as_text(self):
        # Agents are handed the prompt as an argument; a path would be a host
        # path this container cannot resolve.
        self.assertGreater(self.f["prompt_len"], 0)
        self.assertTrue(self.f["prompt_names_task"])

    def test_the_first_attempt_carries_no_feedback(self):
        self.assertFalse(self.f["has_feedback"])

    def test_it_knows_which_cell_it_is(self):
        self.assertEqual(self.f["cell_id"], self.CID)

    def test_it_has_its_own_home(self):
        self.assertTrue(self.f["has_own_home"])

    def test_the_authorable_surface_is_writable(self):
        self.assertTrue(self.f["sees_app"])
        self.assertTrue(self.f["app_writable"])

    def test_the_fixed_surface_is_not(self):
        self.assertTrue(self.f["sees_todo"])
        self.assertFalse(self.f["todo_writable"])

    def test_it_gets_the_task_and_the_docs(self):
        self.assertTrue(self.f["sees_todo"])
        self.assertTrue(self.f["sees_docs"])

    def test_it_cannot_reach_the_verify_surface(self):
        # The anti-gaming claim, checked from inside the container.
        for key in ("sees_harness", "sees_instruments", "sees_workspaces",
                    "sees_repo_root"):
            self.assertFalse(self.f[key], key)


class TestTheGreenPath(ContractTestCase):

    PLAN = "green"


    def test_the_cell_greens_on_the_first_attempt(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr[-2000:])
        self.assertEqual(self.iters(), ["green"])

    def test_the_ledger_records_the_whole_attempt_in_order(self):
        self.assertEqual(self.events(),
                         ["PREPARED", "IMPL", "START", "CKPT", "AGENT",
                          "VERIFY_READY", "SHAPE", "CKPT", "ITER", "END"])

    def test_the_model_transitions_are_the_legal_green_sequence(self):
        self.assertEqual(self.transitions(),
                         ["Admit", "AcquireVerify", "VerifyGreen"])

    def test_the_attempt_is_bracketed_by_checkpoints(self):
        ck = [l for l in (self.ws / "iterations.log").read_text().splitlines()
              if "\tCKPT\t" in l]
        self.assertEqual(len(ck), 2)
        self.assertIn("pre tree=", ck[0])
        self.assertIn("post tree=", ck[1])

    def test_the_agents_work_moved_the_tree(self):
        import re
        ck = [l for l in (self.ws / "iterations.log").read_text().splitlines()
              if "\tCKPT\t" in l]
        pre, post = (re.search(r"tree=(\w+)", c).group(1) for c in ck)
        self.assertNotEqual(pre, post)

    def test_a_green_cell_is_sealed_read_only(self):
        self.assertIn("verdict=green", (self.ws / ".sealed").read_text())
        self.assertFalse(os.access(self.ws / ".sealed", os.W_OK))

    def test_the_heartbeat_is_cleared_on_the_way_out(self):
        self.assertFalse((self.ws / ".loop").exists())

    def test_running_a_sealed_cell_again_is_refused(self):
        again = self.run_cell("green")
        self.assertEqual(again.returncode, 46)
        self.assertIn("is sealed", again.stderr)
        self.assertIn("verdict=green", again.stderr)


class TestFailThenGreen(ContractTestCase):
    """The loop's reason to exist: an attempt fails, the agent is told why, and
    the next attempt uses it."""

    PLAN = "fail,green"


    def test_it_takes_two_attempts(self):
        self.assertEqual(self.iters(), ["fail", "green"])

    def test_the_agent_ran_twice(self):
        self.assertEqual(len(self.findings()), 2)

    def test_the_second_attempt_is_told_what_failed(self):
        second = self.findings()[1]
        self.assertTrue(second["has_feedback"], "attempt 2 retried blind")
        self.assertTrue(second["names_stage"])
        self.assertTrue(second["has_verify_tail"])

    def test_the_transitions_show_a_failure_then_a_pass(self):
        self.assertEqual(self.transitions(),
                         ["Admit", "AcquireVerify", "VerifyFail",
                          "AcquireVerify", "VerifyGreen"])

    def test_every_attempt_is_bracketed(self):
        ck = [l for l in (self.ws / "iterations.log").read_text().splitlines()
              if "\tCKPT\t" in l]
        self.assertEqual(len(ck), 4)

    def test_only_the_last_attempt_seals(self):
        self.assertIn("verdict=green", (self.ws / ".sealed").read_text())
        self.assertIn("attempts=2", (self.ws / ".sealed").read_text())


class TestTheNoEditGuard(ContractTestCase):
    """An attempt that rebuilds byte-for-byte what was already judged is spent
    WITHOUT a verify, and the next prompt must say so."""

    PLAN = "fail,noedit,green"


    def test_the_no_edit_attempt_is_recorded_and_spent(self):
        self.assertIn("NOEDIT", self.events())
        self.assertEqual(self.iters(), ["fail", "fail", "green"])

    def test_the_verify_did_not_run_for_it(self):
        # Three attempts, two verifies: the no-edit one is skipped.
        self.assertEqual(self.verify_calls(), 2)

    def test_the_agent_is_told_it_edited_nothing(self):
        self.assertTrue(self.findings()[2]["says_no_edit"])


class TestOutOfSurfaceEdits(ContractTestCase):
    """A fixed file is infrastructure the agent cannot write. An edit is
    reverted before the verify, logged, and named in the next prompt."""

    PLAN = "heal,green"


    def test_the_edit_is_reverted_and_logged(self):
        self.assertIn("HEAL", self.events())

    def test_the_fixed_file_is_back_to_its_seeded_content(self):
        readme = (self.ws / "artifacts" / "README.md").read_text()
        self.assertNotIn("testagent", readme)

    def test_the_agent_is_told_the_edit_had_no_effect(self):
        self.assertTrue(self.findings()[1]["says_heal"])


class TestBudgetExhaustion(ContractTestCase):
    """An agent that never solves spends the budget and the cell is sealed on
    the failure — attempts-to-green is the dependent variable, so the count
    must be exact."""

    PLAN = "fail"
    TIMEOUT = 1800


    def test_it_spends_exactly_the_budget(self):
        self.assertEqual(len(self.iters()), 10)
        self.assertEqual(self.iters()[-1], "budget")

    def test_the_agent_ran_once_per_attempt(self):
        self.assertEqual(len(self.findings()), 10)

    def test_the_cell_is_sealed_as_budget_exhausted(self):
        self.assertIn("verdict=budget", (self.ws / ".sealed").read_text())
        self.assertIn("attempts=10", (self.ws / ".sealed").read_text())

    def test_the_ledger_ends_not_green(self):
        self.assertIn("green=false",
                      (self.ws / "iterations.log").read_text().splitlines()[-1])


class TestAUsageLimitCostsNoAttempt(ContractTestCase):
    """A usage wall is not a failed build. The SAME attempt is retried, so
    attempts-to-green — the dependent variable — is unaffected by an account's
    billing state."""

    PLAN = "limit,green"

    def test_the_agent_ran_twice_for_one_attempt(self):
        self.assertEqual(len(self.findings()), 2)
        self.assertEqual(self.iters(), ["green"])

    def test_the_wait_is_recorded(self):
        self.assertTrue(any("limit" in w for w in self.waits()), self.waits())

    def test_the_limited_invocation_is_never_verified(self):
        self.assertEqual(self.verify_calls(), 1)

    def test_the_triggering_evidence_survives_the_retry(self):
        # The retry rewrites agent.attempt-1.log, so the log that PROVED the
        # limit has to be rotated aside or the wait is unexplainable later.
        self.assertTrue(list(self.ws.glob("agent.attempt-1.wait-*.log")))

    def test_it_still_greens_on_the_first_attempt(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr[-2000:])
        self.assertIn("attempts=1", (self.ws / ".sealed").read_text())


class TestASilentAgentIsRetried(ContractTestCase):
    """A CLI that dies before reaching the model produces no output at all.
    That is not a failed build either — retry the same attempt."""

    PLAN = "silent,green"

    def test_the_attempt_survives_the_silent_invocation(self):
        self.assertEqual(self.iters(), ["green"])
        self.assertEqual(len(self.findings()), 2)

    def test_the_retry_says_why(self):
        self.assertTrue(any("no output" in w for w in self.waits()), self.waits())


class TestASilentAgentEventuallyHalts(ContractTestCase):
    """...but a permanently broken CLI must not retry for the life of the
    fleet. After the cap it HALTs, and the exit code is the one `conduct`
    treats as SYSTEMIC — a broken CLI stops the queue instead of burning it."""

    PLAN = "silent"

    def test_it_halts_with_the_systemic_exit_code(self):
        self.assertEqual(self.p.returncode, 42, self.p.stderr[-2000:])

    def test_it_retries_the_cap_and_no_further(self):
        # FAULT_RETRIES retries, then the invocation that trips the cap.
        self.assertEqual(len(self.findings()), self.FAULT_RETRIES + 1)
        self.assertEqual(len(self.waits()), self.FAULT_RETRIES)

    def test_the_ledger_names_the_fault(self):
        self.assertIn("HALT", self.events())
        self.assertIn("agent-fault",
                      (self.ws / "iterations.log").read_text())

    def test_no_attempt_is_spent_and_the_cell_is_not_sealed(self):
        # The cell must resume where it stopped once the CLI is fixed.
        self.assertEqual(self.iters(), [])
        self.assertFalse((self.ws / ".sealed").exists())

    def test_the_verify_never_ran(self):
        self.assertEqual(self.verify_calls(), 0)


class TestACrashingAgentSpendsItsAttempt(ContractTestCase):
    """An agent that errors OUT LOUD has reached the model and failed — that is
    a build, and it costs an attempt. Only silence is a harness-side fault."""

    PLAN = "crash,green"

    def test_the_crash_costs_an_attempt(self):
        self.assertEqual(self.iters(), ["fail", "green"])
        self.assertEqual(len(self.findings()), 2)

    def test_the_crash_was_not_treated_as_a_retryable_fault(self):
        self.assertEqual(self.waits(), [])

    def test_the_crashed_attempt_was_still_judged(self):
        self.assertEqual(self.verify_calls(), 2)


class TestASlowAgent(ContractTestCase):
    """An attempt has no time limit — it ends when the agent says it is done.
    Nothing in the loop may reap a build for taking a while."""

    PLAN = "slow:20"

    def test_it_greens_despite_the_wait(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr[-2000:])
        self.assertEqual(self.iters(), ["green"])


if __name__ == "__main__":
    unittest.main()


class TestAWeeklyWallCostsNoAttempt(ContractTestCase):
    """The Claude CLI reports a weekly cap as a SUCCESSFUL run whose result
    is an error: exit 0, nothing edited, `api_error_status` 429. That is the
    wall that once burned ten attempts in twenty seconds."""

    PLAN = "limit429,green"

    def test_the_agent_ran_twice_for_one_attempt(self):
        self.assertEqual(len(self.findings()), 2)
        self.assertEqual(self.iters(), ["green"])

    def test_the_wait_is_recorded_and_nothing_is_charged(self):
        self.assertTrue(any("limit" in w for w in self.waits()), self.waits())
        self.assertNotIn("NOEDIT", self.events())

    def test_the_limited_invocation_is_never_verified(self):
        self.assertEqual(self.verify_calls(), 1)

    def test_the_triggering_evidence_survives_the_retry(self):
        self.assertTrue(list(self.ws.glob("agent.attempt-1.wait-*.log")))

    def test_it_still_greens_on_the_first_attempt(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr[-2000:])
        self.assertIn("attempts=1", (self.ws / ".sealed").read_text())


class TestAnAuthWallHaltsTheCell(ContractTestCase):
    """A login wall is not lifted by waiting. The cell halts with the
    systemic exit code, spends nothing, and resumes where it stopped."""

    PLAN = "auth"

    def test_it_halts_with_the_systemic_exit_code(self):
        self.assertEqual(self.p.returncode, 42, self.p.stderr[-2000:])

    def test_it_is_never_retried(self):
        self.assertEqual(len(self.findings()), 1)
        self.assertEqual(self.waits(), [])

    def test_the_ledger_names_the_wall(self):
        self.assertIn("HALT", self.events())
        self.assertIn("auth-wall", (self.ws / "iterations.log").read_text())

    def test_no_attempt_is_spent_and_the_cell_is_not_sealed(self):
        self.assertEqual(self.iters(), [])
        self.assertFalse((self.ws / ".sealed").exists())

    def test_the_verify_never_ran(self):
        self.assertEqual(self.verify_calls(), 0)
