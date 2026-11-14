"""Validator rules must not claim more than they check, and pausing must not
switch validation off.

These are the silent ones: nothing fails, a verdict is simply recorded as if a
rule had been applied when it could not fire.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs, OrchTmpCase, patch_plane, at_workspace, use_workspace


class TestCeilingRulesAreHonest(unittest.TestCase):
    """One ceiling is recorded per cell, so any rule needing a per-attempt
    HISTORY cannot fire. Publishing it anyway is the defect.

    Asserted on the emitted validation.json, not by grepping the source: what
    matters is what a reader of that file is told.
    """

    def _validate(self, ceiling):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "sonnet_high_pulumi_sealed_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            ts = "2026-07-30T09:00:00Z"
            (ws / "iterations.log").write_text(
                f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n"
                f"{ts}\tEND\tgreen=true\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            (ws / "metrics.json").write_text(json.dumps(dict(
                ceiling_qps=ceiling, load_total=8000, load_errors=0,
                scaling_ok=True, k6_available=True, load_ran=True)))
            (ws / "verify.log").write_text("ok\n")
            (ws / "run_cell.log").write_text("ok\n")
            with at_workspace(Path(d), Path(d)):
                return runs.validate_ws(ws)

    def test_a_healthy_single_ceiling_produces_no_taint(self):
        doc = self._validate(80)
        self.assertEqual(doc["taints"], [])

    def test_no_unstable_host_taint_can_fire_on_one_sample(self):
        """`min(x) < 0.6 * max(x)` over a one-element list is `x < 0.6x` —
        false for every positive ceiling, so the rule never applied while
        validation.json still recorded a rule set as if it had."""
        for c in (80, 55, 10):
            with self.subTest(ceiling=c):
                doc = self._validate(c)
                self.assertFalse(
                    any("fluctuat" in t for t in doc["taints"]),
                    "a range rule fired on a single sample")

    def test_a_low_ceiling_is_reported_once_not_as_taint_and_warn(self):
        """`hist = deciding` was an ALIAS, so the warn fired on exactly the
        same condition as the taint above it — one finding, reported twice."""
        doc = self._validate(10)
        self.assertEqual(len([t for t in doc["taints"] if "ceiling" in t]), 1)
        self.assertEqual([w for w in doc["warns"] if "qps <" in w], [])

    def test_no_fictitious_range_field_is_published(self):
        """`ceilings_all_min_max` was always [x, x]: it aliased the single
        deciding ceiling while its name promised a range across attempts."""
        doc = self._validate(80)
        self.assertNotIn("ceilings_all_min_max", doc)
        self.assertEqual(doc["ceilings_deciding"], [80])

    def test_rule_set_bumped_so_old_documents_stay_distinguishable(self):
        self.assertEqual(self._validate(80)["rule_set"], 10)


class TestPreLoadFailureIsNotTainted(unittest.TestCase):
    """A cell that dies before the load stage records load_ran: false, because
    write_metrics runs from an EXIT trap on every path. Tainting that removes
    the HARDEST authoring failures from the dataset — the cells that could not
    get a deploy up — while keeping everything that survived to be load-tested.
    That biases the result toward the hypothesis. Operator decision
    2026-07-30: it is a failure, not a rig fault."""

    def _validate(self, **metrics):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "sonnet_high_pulumi_sealed_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            ts = "2026-07-30T09:00:00Z"
            (ws / "iterations.log").write_text(
                f"{ts}\tITER\tfail\tattempt=1 stage=deploy\n"
                f"{ts}\tEND\tgreen=false\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            base = dict(ceiling_qps=80, k6_available=True)
            base.update(metrics)
            (ws / "metrics.json").write_text(json.dumps(base))
            (ws / "verify.log").write_text("FAIL[deploy]: image build failed\n")
            (ws / "run_cell.log").write_text("FAIL[deploy]: image build failed\n")
            with at_workspace(Path(d), Path(d)):
                return runs.validate_ws(ws)

    def test_never_reaching_load_is_not_a_taint(self):
        doc = self._validate(load_ran=False, load_total=0, load_errors=0)
        self.assertEqual(doc["verdict"], "VALID")
        self.assertFalse(any("load phase never ran" in t for t in doc["taints"]))

    def test_it_is_recorded_as_a_warn_so_the_reader_is_told(self):
        doc = self._validate(load_ran=False, load_total=0, load_errors=0)
        self.assertTrue(any("load phase never ran" in w for w in doc["warns"]),
                        "the absence of delivered load must still be reported")

    def test_a_partial_numeric_load_IS_still_tainted(self):
        """Distinct case: the load ran but under-delivered, so the scaling
        verdict rests on a fraction of the designed traffic."""
        doc = self._validate(load_ran=100, load_total=8000, load_errors=0)
        self.assertTrue(any("of the designed load ran" in t for t in doc["taints"]))

    def test_k6_false_on_a_pre_e2e_death_is_not_a_taint(self):
        """The harness inits k6_available=false and only consults k6 on an
        e2e-green build, so a cell whose final attempt dies pre-e2e records
        false without k6 ever being checked (haiku_..._r16, stage=store,
        while k6 served 8047 requests to a sibling the same hour)."""
        doc = self._validate(k6_available=False, e2e_green=False,
                             load_ran=False, load_total=None, load_errors=None)
        self.assertEqual(doc["verdict"], "VALID")
        self.assertFalse(any("k6 unavailable" in t for t in doc["taints"]))

    def test_k6_missing_on_a_green_build_IS_still_tainted(self):
        """The real case the rule exists for: load was due and never ran."""
        doc = self._validate(k6_available=False, e2e_green=True,
                             load_ran=False, load_total=None, load_errors=None)
        self.assertTrue(any("k6 unavailable" in t for t in doc["taints"]))


class TestPauseDoesNotDisableValidation(OrchTmpCase):
    """reconcile skips any cell whose intent is not "run", and that check sits
    ABOVE its mandatory-at-DONE validation. So pausing "all" — which drain and
    stop-all both do — silently stopped the fleet being validated."""
    # temp tree + WS and scheduling-plane patching inherited from OrchTmpCase.

    def _cell(self, cid, ledger_lines):
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True)
        (d / "iterations.log").write_text(ledger_lines)
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
        return d

    def test_terminal_cells_are_not_paused(self):
        ts = "2026-07-30T09:00:00Z"
        done = "sonnet_high_pulumi_sealed_apidocs_T1_r1"
        live = "sonnet_high_pulumi_sealed_apidocs_T1_r2"
        self._cell(done, f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n"
                         f"{ts}\tEND\tgreen=true\n")
        self._cell(live, f"{ts}\tPREPARED\t{live}\tby=prepare_cell\n")

        written = runs.conduct.Conduct.request_pause([done, live], "drain")

        self.assertNotIn(done, written, "paused a finished cell")
        self.assertFalse((self.ws / done / ".paused").exists())
        self.assertIn(live, written)
        self.assertTrue((self.ws / live / ".paused").exists())

    def test_a_drain_leaves_finished_cells_validatable(self):
        """The consequence: after `pause all`, a DONE cell must still have
        intent 'run' so reconcile's mandatory validation still reaches it."""
        ts = "2026-07-30T09:00:00Z"
        done = "sonnet_high_kedap_sealed_apidocs_T1_r1"
        self._cell(done, f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n"
                         f"{ts}\tEND\tgreen=true\n")
        runs.conduct.Conduct.request_pause([done], "drain")
        self.assertEqual(runs.experiment.exp().cell(done, workspaces=self.ws).declared_intent()[0], "run")


class TestSpecsSurviveAConductCrash(unittest.TestCase):
    """A spec's state is its location, and every move is one rename: there is
    no window where a spec is neither queued nor claimed, so a conduct killed
    at any instant leaves the backlog exactly as complete as it found it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patch_plane(self, Path(self._tmp.name))
        # WS as well as the plane: enqueue refuses a SEALED cell, and these
        # specs name real cids — against the live tree this class would be
        # asserting the seal state of a finished cell instead of spec mechanics.
        use_workspace(self, Path(self._tmp.name) / "ws")
        self.addCleanup(self._tmp.cleanup)
        self.spec = {"task": "T1", "variant": "pulumi_sealed_apidocs",
                     "rep": 1, "fresh": False}

    def test_a_claimed_spec_is_still_there_after_a_crash(self):
        runs.queues.enqueue("sonnet", self.spec)
        runs.queues.claim("sonnet", runs.queues.lane_specs("sonnet")[0])
        # nothing else runs: the "crash" is simply not doing anything more
        claims = runs.queues.running_specs("sonnet")
        self.assertEqual(len(claims), 1)
        self.assertEqual(runs.queues.read_spec(claims[0])["rep"], 1)

    def test_a_released_spec_returns_to_the_head(self):
        runs.queues.enqueue("sonnet", dict(self.spec, rep=2))
        runs.queues.enqueue("sonnet", self.spec)
        p = runs.queues.lane_specs("sonnet")[1]          # the r1 spec
        runs.queues.release("sonnet", runs.queues.claim("sonnet", p))
        self.assertEqual([runs.queues.read_spec(x)["rep"]
                          for x in runs.queues.lane_specs("sonnet")], [1, 2])
        self.assertEqual(runs.queues.running_specs("sonnet"), [])

    def test_one_cid_cannot_be_queued_twice(self):
        self.assertIsNotNone(runs.queues.enqueue("sonnet", self.spec))
        self.assertIsNone(runs.queues.enqueue("sonnet", self.spec),
                          "a lane holds at most one spec per cid")


class TestEpochWriter(OrchTmpCase):
    """The EPOCH lines are the replay's starting state. If they misdescribe
    the fleet, every later verdict is judged against a fiction — the same
    failure as resetting the log at the wrong moment, just written down."""

    def setUp(self):
        super().setUp()   # temp tree + WS and the scheduling plane patched
        for p_ in (mock.patch.object(runs.host, "loop_parents", return_value={}),
                   mock.patch.object(runs.host, "containers", return_value=[])):
            p_.start()
            self.addCleanup(p_.stop)

    def _cell(self, cid, ledger_lines, paused=None, cancelled=False):
        d = self.ws / cid
        (d / "artifacts").mkdir(parents=True)
        (d / "iterations.log").write_text(ledger_lines)
        (d / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
        if paused: (d / ".paused").write_text(f"{paused} by=operator\n")
        if cancelled: (d / ".cancelled").write_text("killed\n")
        return d

    def test_domains_match_the_spec(self):
        """Values must be in Runs.tla's declared domains or TypeOK fails on
        the very first state."""
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r2",
                   f"{ts}\tITER\tfail\tattempt=1 stage=scaling\n{ts}\tEND\tgreen=false\n",
                   paused="drain")
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r3",
                   f"{ts}\tPREPARED\tx\tby=prepare_cell\n", cancelled=True)
        rows = {c["cid"]: c for c in runs.experiment.exp().observed_epoch()}
        self.assertEqual(len(rows), 3)
        for c in rows.values():
            self.assertIn(c["outcome"], {"none", "green", "failed", "revoked"})
            self.assertIn(c["intent"], {"run", "paused", "killed"})
            self.assertIn(c["loop"], {"none", "idle", "agent", "verify"})
            self.assertGreaterEqual(c["attempts"], 0)

    def test_verdict_intent_and_attempts_are_read_correctly(self):
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r2",
                   f"{ts}\tITER\tfail\tattempt=1 stage=scaling\n"
                   f"{ts}\tITER\tfail\tattempt=2 stage=scaling\n", paused="roster")
        rows = {c["cid"]: c for c in runs.experiment.exp().observed_epoch()}
        a = rows["sonnet_high_pulumi_sealed_apidocs_T1_r1"]
        self.assertEqual((a["outcome"], a["intent"], a["attempts"]), ("green", "run", 1))
        b = rows["sonnet_high_pulumi_sealed_apidocs_T1_r2"]
        self.assertEqual((b["outcome"], b["intent"], b["attempts"]), ("none", "paused", 2))

    def test_cancelled_beats_paused(self):
        """kill is durable intent; a killed cell carries both markers."""
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tPREPARED\tx\tby=prepare_cell\n",
                   paused="killed", cancelled=True)
        self.assertEqual(runs.experiment.exp().observed_epoch()[0]["intent"], "killed")

    def test_every_cell_is_one_line_of_one_block(self):
        """Cells at Init too: the replay starts from the last EPOCH block, so
        the block is never empty while there are cells."""
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1", "")          # Init
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r2",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        runs.experiment.exp().reset_trace(dry_run=False)
        lines = runs.experiment.exp().workspace.transitions.read_text().splitlines()
        self.assertTrue(lines[0].startswith("#"))
        self.assertEqual([l.split("\t")[1] for l in lines[1:]], ["EPOCH", "EPOCH"])
        self.assertIn("_r1\toutcome=none intent=run loop=none attempts=0", lines[1])
        self.assertIn("_r2\toutcome=green", lines[2])

    def test_epoch_lines_parse_as_ordinary_transitions(self):
        """tla_verify's parser splits on tabs and regexes k=v out of field 4.
        The EPOCH line must satisfy that shape or the reader cannot consume it."""
        import re
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        runs.experiment.exp().reset_trace(dry_run=False)
        line = [l for l in runs.experiment.exp().workspace.transitions.read_text().splitlines()
                if l and not l.startswith("#")][0]
        f = line.split("\t")
        self.assertGreaterEqual(len(f), 4)
        self.assertEqual(f[1], "EPOCH")
        kv = dict(re.findall(r"(\S+)=(\S+)", f[3]))
        self.assertEqual(set(kv), {"outcome", "intent", "loop", "attempts",
                                   "slot", "verify"})

    def test_dry_run_writes_nothing(self):
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        runs.experiment.exp().workspace.transitions.write_text("original\n")
        runs.experiment.exp().reset_trace(dry_run=True)
        self.assertEqual(runs.experiment.exp().workspace.transitions.read_text(), "original\n")

    def test_an_existing_log_is_kept_and_the_block_appended(self):
        ts = "2026-07-30T09:00:00Z"
        self._cell("sonnet_high_pulumi_sealed_apidocs_T1_r1",
                   f"{ts}\tITER\tgreen\tattempt=1 shapes=all\n{ts}\tEND\tgreen=true\n")
        runs.experiment.exp().workspace.transitions.write_text("prior history\n")
        runs.experiment.exp().reset_trace(dry_run=False)
        lines = runs.experiment.exp().workspace.transitions.read_text().splitlines()
        self.assertEqual(lines[0], "prior history")
        self.assertEqual(lines[2].split("\t")[1], "EPOCH")
        self.assertEqual(list(self.plane.glob("transitions.archived-*.log")), [])


if __name__ == "__main__":
    unittest.main()


class TestAnAttemptChargedOnAWallIsTainted(unittest.TestCase):
    """Rule 11. Fifteen cells were sealed DONE·failed and validated VALID on
    2026-08-25 with every charged attempt a two-second 429 result line. The
    driver now waits on those; the validator says so when one slips through."""

    def _validate(self, iters, logs, extra_metrics=None):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "sonnet_high_pulumi_access_apidocs_T1_r18"
            (ws / "artifacts").mkdir(parents=True)
            ts = "2026-08-25T20:55:24Z"
            (ws / "iterations.log").write_text(
                "".join(f"{ts}\tITER\t{r}\tattempt={n} stage=no-edit tree=abc\n"
                        for n, r in iters)
                + f"{ts}\tEND\tgreen=false\n")
            for n, text in logs.items():
                (ws / f"agent.attempt-{n}.log").write_text(text)
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            (ws / "metrics.json").write_text(json.dumps(dict(
                ceiling_qps=59, load_total=0, load_errors=0, load_ran=False,
                k6_available=True, e2e_green=False, **(extra_metrics or {}))))
            (ws / "verify.log").write_text("")
            with at_workspace(Path(d), Path(d)):
                return runs.validate_ws(ws)

    def wall(self):
        from fae.testagent import LIMIT429_LINES
        return "\n".join(LIMIT429_LINES) + "\n"

    def test_the_429_result_line_taints_the_charged_attempt(self):
        doc = self._validate([(1, "fail"), (2, "budget")],
                             {1: self.wall(), 2: self.wall()})
        self.assertEqual(doc["verdict"], "TAINTED")
        self.assertEqual(len([t for t in doc["taints"] if "provider" in t]), 2)
        self.assertIn("weekly limit", doc["taints"][0])

    def test_an_auth_wall_taints_too(self):
        from fae.testagent import AUTH_LINE
        doc = self._validate([(1, "fail")], {1: AUTH_LINE + "\n"})
        self.assertTrue(any("provider auth wall" in t for t in doc["taints"]))

    def test_a_gemini_eligibility_eof_on_an_untouched_tree_taints(self):
        """gemini_high_kedap_sealed_apidocs_T1_r14 attempt 1: the Gemini CLI
        failed its eligibility call with EOF, printed one line, exited; the
        driver charged the attempt as no-edit and this rule let it pass."""
        line = ('Error: Eligibility check failed: failed to get load code '
                'assist response: Post "https://daily-cloudcode-pa.googleapis'
                '.com/v1internal:loadCodeAssist": EOF\n')
        doc = self._validate([(1, "fail")], {1: line})
        hits = [t for t in doc["taints"] if "provider limit wall" in t]
        self.assertEqual(len(hits), 1)
        self.assertIn("attempt 1", hits[0])
        self.assertIn("Eligibility check failed", hits[0])
        self.assertEqual(doc["verdict"], "TAINTED")

    def test_a_real_failure_whose_prose_mentions_limits_is_not(self):
        doc = self._validate(
            [(1, "fail")],
            {1: '{"type":"result","subtype":"success","is_error":false,'
                '"result":"added a retry for the 429 rate limit path"}\n'})
        self.assertEqual([t for t in doc["taints"] if "provider" in t], [])

    def test_a_missing_log_is_no_evidence(self):
        doc = self._validate([(1, "fail")], {})
        self.assertEqual([t for t in doc["taints"] if "provider" in t], [])

    def test_rule_set_is_bumped(self):
        self.assertEqual(self._validate([(1, "fail")], {})["rule_set"], 10)

    def test_a_verify_that_changed_the_cells_record_taints_the_cell(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "sonnet_high_pulumi_access_apidocs_T1_r18"
            (ws / "artifacts").mkdir(parents=True)
            ts = "2026-08-25T20:55:24Z"
            (ws / "iterations.log").write_text(
                f"{ts}\tALERT\tsonnet_high_pulumi_access_apidocs_T1_r18\tattempt=2\t"
                "INTEGRITY the verify changed the cell's record: iterations.log\n"
                f"{ts}\tITER\tfail\tattempt=2 stage=e2e\n{ts}\tEND\tgreen=false\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
            (ws / "metrics.json").write_text("{}")
            (ws / "verify.log").write_text("")
            with at_workspace(Path(d), Path(d)):
                doc = runs.validate_ws(ws)
        hits = [t for t in doc["taints"] if "changed the cell's record" in t]
        self.assertEqual(len(hits), 1)
        self.assertIn("iterations.log", hits[0])
        self.assertEqual(doc["verdict"], "TAINTED")

    def test_an_edited_attempt_is_judged_by_the_structured_line_only(self):
        """A plain transcript is full of the task's own vocabulary (the
        agent writes rate-limit code); without a structured result line it
        decides only for an untouched tree. 222 real cells, greens among
        them, were tainted before this gate."""
        prose = 'raise HTTPException(status_code=429, detail="rate limit")\n'
        ts = "2026-08-25T20:55:24Z"
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "dsv4f_high_kedap_sealed_apidocs_T1_r1"
            (ws / "artifacts").mkdir(parents=True)
            (ws / "iterations.log").write_text(
                f"{ts}\tITER\tfail\tattempt=1 stage=scaling e2e=7/7 tree=abc\n"
                f"{ts}\tITER\tfail\tattempt=2 stage=no-edit tree=abc\n"
                f"{ts}\tEND\tgreen=false\n")
            (ws / "agent.attempt-1.log").write_text(prose)
            (ws / "agent.attempt-2.log").write_text(
                "Error: Individual quota reached. Resets in 3h.\n")
            (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\n")
            (ws / "metrics.json").write_text(json.dumps(dict(
                ceiling_qps=59, load_total=8000, load_errors=0, load_ran=True,
                k6_available=True, e2e_green=True)))
            (ws / "verify.log").write_text("")
            with at_workspace(Path(d), Path(d)):
                doc = runs.validate_ws(ws)
        hits = [t for t in doc["taints"] if "provider" in t]
        self.assertEqual(len(hits), 1)
        self.assertIn("attempt 2", hits[0])


class TestTheJunctionRules(unittest.TestCase):
    """Rules 12-16: one-arm rig defects the 2026-08-27 scan found, matched by
    the defect's own signature in the rig-written logs. Asserted on the
    emitted validation.json and on the seal's taint lines."""

    TS = "2026-07-30T09:00:00Z"

    def cell(self, d, cid, iters, green, sealed="2026-08-10T00:00:00Z",
             deploy_log="", service_log="", k8s=None):
        ws = Path(d) / cid
        (ws / "artifacts").mkdir(parents=True)
        (ws / "iterations.log").write_text(
            "".join(f"{self.TS}\tITER\t{r}\tattempt={n} {rest}\n" for n, r, rest in iters)
            + f"{self.TS}\tEND\tgreen={'true' if green else 'false'}\n")
        (ws / "cell.env").write_text("ATTEMPT_BUDGET=10\nAGENT_MODEL=5\n")
        (ws / "metrics.json").write_text(json.dumps(dict(
            ceiling_qps=60, load_total=8000, load_errors=0,
            scaling_ok=green, k6_available=True, load_ran=True)))
        (ws / "verify.log").write_text("ok\n")
        (ws / "run_cell.log").write_text("ok\n")
        (ws / "deploy.log").write_text(deploy_log)
        (ws / "service.deploy.log").write_text(service_log)
        for name, text in (k8s or {}).items():
            (ws / "artifacts" / "k8s").mkdir(exist_ok=True)
            (ws / "artifacts" / "k8s" / name).write_text(text)
        seal = ws / ".sealed"
        seal.write_text(f"sealed={sealed}\tverdict={'green' if green else 'budget'}\tattempts=3\tby=test\n")
        seal.chmod(0o444)
        return ws

    def validate(self, ws):
        with at_workspace(ws.parent, ws.parent):
            return runs.validate_ws(ws)

    def test_rule_12_a_charged_store_failure_taints(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_pulumi_sealed_apidocs_T1_r1",
                           [(1, "fail", "stage=scaling e2e=7/7"), (2, "fail", "stage=store e2e=0/0"),
                            (3, "budget", "stage=scaling e2e=7/7")], green=False)
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "TAINTED")
        self.assertTrue(any(t.startswith("rule 12: attempt(s) 2 ") for t in doc["taints"]), doc)
        self.assertEqual(doc["rule_set"], 10)

    def test_rule_13_an_unrewritten_app_host_on_the_last_attempt_taints(self):
        log = ("[deploy-conv] cluster scrapes the LOCAL app at 1.2.3.4:8080\n"
               "[deploy-conv]   NOTE: no http://<host>:8080/metrics scrape target found in any k8s manifest\n")
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_kedap_sealed_apidocs_T1_r1",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, deploy_log=log)
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "TAINTED")
        self.assertTrue(any(t.startswith("rule 13:") for t in doc["taints"]), doc)

    def test_rule_13_a_rewritten_last_attempt_is_not_tainted(self):
        log = ("[deploy-conv]   NOTE: no http://<host>:8080/metrics scrape target found in any k8s manifest\n"
               "[deploy-conv]   rewrote app scrape target -> 1.2.3.4 in k8s/prometheus.yaml\n")
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_kedap_sealed_apidocs_T1_r2",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, deploy_log=log)
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "VALID")

    @staticmethod
    def prom_targets(ws, url, health="down"):
        (ws / "cluster-diag").mkdir()
        (ws / "cluster-diag" / "prom-targets.json").write_text(json.dumps({
            "status": "success",
            "data": {"activeTargets": [{"scrapeUrl": url, "health": health}]}}))

    def test_rule_13_a_scraper_left_with_the_placeholder_taints_without_the_note(self):
        log = "[deploy-conv]   rewrote app scrape target -> 1.2.3.4 in k8s/prometheus.yaml\n"
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_kedap_sealed_apidocs_T1_r4",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, deploy_log=log)
            self.prom_targets(ws, "http://APP_HOST:8080/metrics/prometheus")
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "TAINTED")
        self.assertTrue(any(t.startswith("rule 13: the scraper's last state still targets "
                                         "http://APP_HOST:8080/metrics/prometheus")
                            for t in doc["taints"]), doc)

    def test_rule_13_a_scraper_on_a_rewritten_target_is_not_the_signature(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_kedap_sealed_apidocs_T1_r5",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False)
            self.prom_targets(ws, "http://192.168.65.254:8080/metrics", health="up")
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "VALID")
        self.assertFalse([w for w in doc["warns"] if w.startswith("rule 13:")], doc)

    def test_rule_13_a_green_cell_with_a_placeholder_diag_keeps_its_verdict_and_is_warned(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "opus_high_kedap_sealed_apidocs_T1_r6",
                           [(1, "fail", "stage=scaling e2e=7/7"), (2, "green", "shapes=all")], green=True)
            self.prom_targets(ws, "http://APP_HOST:8080/metrics")
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "VALID")
        self.assertTrue(any(w.startswith("rule 13: the scraper's last state") for w in doc["warns"]), doc)

    def test_rule_13_the_fixed_bringups_note_is_not_the_signature(self):
        log = "[deploy-conv]   NOTE: no APP_HOST placeholder or http://<host>:8080/metrics target found in any k8s manifest\n"
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_kedap_sealed_apidocs_T1_r3",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, deploy_log=log)
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "VALID")

    def test_rule_14_a_cache_deployment_not_named_redis_taints_before_the_fix(self):
        k8s = {"cache.yaml": "kind: Deployment\nmetadata:\n  name: redis-cache\n---\nkind: Deployment\nmetadata:\n  name: prometheus\n"}
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "gemini_high_kedap_access_apidocs_T1_r5",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, k8s=k8s)
            doc = self.validate(ws)
            self.assertTrue(any(t.startswith("rule 14: cache Deployment redis-cache") for t in doc["taints"]), doc)
            ws2 = self.cell(d, "gemini_high_kedap_access_apidocs_T1_r6",
                            [(1, "budget", "stage=scaling e2e=7/7")], green=False, k8s=k8s,
                            sealed="2026-08-28T00:00:00Z")
            self.assertEqual(self.validate(ws2)["verdict"], "VALID")

    def test_rule_15_a_clusterip_collision_taints_a_failed_cell_and_warns_a_green_one(self):
        log = 'The Service "prometheus" is invalid: spec.clusterIPs: Invalid value: ["10.96.0.9"]: failed to allocate IP 10.96.0.9: provided IP is already allocated\n'
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "opus_high_kedap_access_apidocs_T1_r1",
                           [(1, "fail", "stage=deploy e2e=0/0"), (2, "budget", "stage=scaling e2e=7/7")],
                           green=False, deploy_log=log)
            doc = self.validate(ws)
            self.assertTrue(any(t.startswith("rule 15: 1 deploy(s)") and "(1 attempt(s) charged)" in t
                                for t in doc["taints"]), doc)
            ws2 = self.cell(d, "opus_high_kedap_access_apidocs_T1_r2",
                            [(1, "fail", "stage=deploy e2e=0/0"), (2, "green", "e2e=7/7 shapes=all")],
                            green=True, deploy_log=log)
            doc2 = self.validate(ws2)
            self.assertEqual(doc2["verdict"], "VALID")
            self.assertTrue(any(w.startswith("rule 15:") for w in doc2["warns"]), doc2)

    def test_rule_16_ghost_state_taints_a_pulumi_cell_sealed_before_the_fix(self):
        log = "error:   sdk-v2/provider2.go:520: Error stopping container abc: Error response from daemon: No such container: abc\n"
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "sonnet_high_pulumi_access_apidocs_T1_r5",
                           [(1, "budget", "stage=scaling e2e=7/7")], green=False, service_log=log)
            doc = self.validate(ws)
            self.assertTrue(any(t.startswith("rule 16:") for t in doc["taints"]), doc)
            ws2 = self.cell(d, "sonnet_high_pulumi_access_apidocs_T1_r6",
                            [(1, "budget", "stage=scaling e2e=7/7")], green=False, service_log=log,
                            sealed="2026-08-28T00:00:00Z")
            self.assertEqual(self.validate(ws2)["verdict"], "VALID")

    def test_the_seal_carries_the_taints_and_stays_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "haiku_high_pulumi_sealed_apidocs_T1_r9",
                           [(1, "fail", "stage=store e2e=0/0"), (2, "budget", "stage=scaling e2e=7/7")],
                           green=False)
            self.validate(ws)
            seal = (ws / ".sealed").read_text().splitlines()
            self.assertTrue(seal[0].startswith("sealed="))
            self.assertEqual([l for l in seal if l.startswith("taint=rule 12:")][:1],
                             [l for l in seal if l.startswith("taint=")][:1])
            self.assertEqual((ws / ".sealed").stat().st_mode & 0o777, 0o444)
            # a second validate with the same result leaves the file as it is
            before = (ws / ".sealed").stat().st_mtime_ns
            self.validate(ws)
            self.assertEqual((ws / ".sealed").stat().st_mtime_ns, before)

class TestAnUnreliableFootprintIsSaidSo(TestTheJunctionRules):

    def test_a_disagreeing_sampler_run_warns(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.cell(d, "opus_high_pulumi_sealed_apidocs_T1_r3",
                           [(1, "green", "e2e=7/7 shapes=all")], green=True)
            (ws / "resources.json").write_text(json.dumps(
                {"reliable": False, "scope": "cell", "unreliable_samples": 2,
                 "cross_check_findings": ["fae-dind-x: docker stats 300 MB vs cgroup 600 MB"]}))
            doc = self.validate(ws)
        self.assertEqual(doc["verdict"], "VALID")
        self.assertTrue(any(w.startswith("footprint unreliable (scope=cell, 2 sample(s)") for w in doc["warns"]), doc)
