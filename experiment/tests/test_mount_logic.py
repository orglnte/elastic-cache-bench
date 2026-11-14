"""mount_counts_seen / never_mounted / mounts_matched — the taint discriminators.

These three decide whether a failed cell is a RIG fault (raise it, the datum is
invalid) or an AUTHORING failure (keep it, the datum is the result). Getting
that backwards in either direction corrupts the study: a rig fault scored as an
authoring failure is evidence manufactured against the hypothesis, and an
authoring failure excused as a rig fault is evidence manufactured for it.

Both directions have already happened once — see the docstrings in
experiment/taint.py.
"""
import unittest
from pathlib import Path

from _ctx import ENGINE_TREE, runs, ROOT
from experiment import taint

# Shape of the real scaling-judge line, as written into run_cell.log.
FAIL = ("FAIL[scaling]: expected {exp} mount episode(s) (one per spike), "
        "saw {saw}")


def rc(*pairs):
    """A synthetic run_cell.log body with one judged attempt per pair."""
    return "\n".join(FAIL.format(exp=e, saw=s) for e, s in pairs)


class TestMountCountsSeen(unittest.TestCase):

    def test_empty_text_yields_nothing(self):
        self.assertEqual(taint.mount_counts_seen(""), [])

    def test_reads_the_saw_count_not_the_expected_count(self):
        self.assertEqual(taint.mount_counts_seen(rc((2, 0))), [0])

    def test_one_entry_per_judged_attempt_in_order(self):
        self.assertEqual(taint.mount_counts_seen(rc((2, 0), (2, 1), (2, 3))),
                         [0, 1, 3])

    def test_unjudged_attempts_contribute_nothing(self):
        """Build failures, e2e failures and HALTs never reach the scaling
        judge, so they must not be counted as 'reached the stage and saw 0'."""
        text = ("FAIL[build]: image build failed\n"
                "HALT: rig fault, no attempt burned\n"
                + rc((2, 0)))
        self.assertEqual(taint.mount_counts_seen(text), [0])

    def test_multi_digit_counts(self):
        self.assertEqual(taint.mount_counts_seen(rc((2, 12))), [12])


class TestNeverMounted(unittest.TestCase):

    def test_needs_at_least_two_scaling_attempts(self):
        """A single `saw 0` is ordinary — a first attempt often misses
        entirely. Only an unbroken run of them is a rig signature."""
        self.assertFalse(taint.never_mounted(rc((2, 0))))

    def test_two_consecutive_zeroes_is_the_signature(self):
        self.assertTrue(taint.never_mounted(rc((2, 0), (2, 0))))

    def test_ten_zeroes_is_the_observed_rig_fault(self):
        """A provisioning port that never becomes reachable records `saw 0`
        on every attempt."""
        self.assertTrue(taint.never_mounted(rc(*[(2, 0)] * 10)))

    def test_one_successful_mount_clears_it(self):
        """Mounting once proves the provisioning path works; the remaining
        failures are then the model's timing, not the rig's plumbing."""
        self.assertFalse(taint.never_mounted(rc((2, 0), (2, 0), (2, 1))))

    def test_no_scaling_attempts_is_not_never_mounted(self):
        self.assertFalse(taint.never_mounted(""))
        self.assertFalse(taint.never_mounted("FAIL[build]: nope"))


class TestMountsMatched(unittest.TestCase):

    def test_scaling_ok_short_circuits(self):
        self.assertTrue(taint.mounts_matched({"scaling_ok": True}))

    def test_green_cell_has_no_why(self):
        """Every green passes: `scaling_why` is None on a clean run."""
        self.assertTrue(taint.mounts_matched({"scaling_ok": True,
                                             "scaling_why": None}))

    def test_absent_verdict_is_not_a_match(self):
        """Nothing asserts the mounts were right, so the gate must not open —
        otherwise an unrecorded verdict would read as a passing one."""
        self.assertFalse(taint.mounts_matched({}))
        self.assertFalse(taint.mounts_matched({"scaling_ok": False}))
        self.assertFalse(taint.mounts_matched({"scaling_ok": False,
                                              "scaling_why": ""}))

    def test_count_mismatch_fails(self):
        self.assertFalse(taint.mounts_matched({
            "scaling_ok": False,
            "scaling_why": "expected 2 mount episode(s) (one per spike), saw 0"}))

    def test_count_match_passes(self):
        self.assertTrue(taint.mounts_matched({
            "scaling_ok": False,
            "scaling_why": "expected 2 mount episode(s) (one per spike), saw 2"}))

    def test_timing_violation_fails_even_when_counts_match(self):
        """Mounting the right NUMBER of times at the WRONG MOMENT is still a
        wrong policy — the count check alone would wave it through."""
        for why in ("episode 1 released MID-spike (ts=812)",
                    "episode 2 mounted outside spike window"):
            with self.subTest(why=why):
                self.assertFalse(taint.mounts_matched({"scaling_ok": False,
                                                      "scaling_why": why}))

    def test_load_errors_only_is_the_interesting_pass(self):
        """A cell that mounted correctly and still dropped requests is NOT
        green, so a green-only gate would miss it — and that case is exactly
        the one worth flagging, because the policy did the right thing and the
        requests failed anyway."""
        self.assertTrue(taint.mounts_matched({
            "scaling_ok": False,
            "scaling_why": "load errors 544/8049 = 6.8% > 0%"}))


if __name__ == "__main__":
    unittest.main()


class TestMountReportText(unittest.TestCase):
    """The judge's reports are read from wherever the driver put them: the
    bash tee when it exists, else the per-arrangement verify.log archive —
    and never both, or one attempt's report would count twice."""

    LINE = "FAIL[scaling]: expected 1 mount episode(s) (one per spike), saw {n}\n"

    def _ws(self, d, *, tee=None, arrangements=(), top=None):
        import pathlib
        ws = pathlib.Path(d)
        if tee is not None:
            (ws / "run_cell.log").write_text(tee)
        for i, n in enumerate(arrangements, 1):
            a = ws / "arrangements" / f"{i:02d}-BBS"
            a.mkdir(parents=True)
            (a / "verify.log").write_text(self.LINE.format(n=n))
        if top is not None:
            (ws / "verify.log").write_text(top)
        return ws

    def test_a_bash_era_cell_reads_its_tee_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d, tee=self.LINE.format(n=0) * 3, arrangements=(2,),
                          top=self.LINE.format(n=2))
            self.assertEqual(taint.mount_counts_seen(taint.mount_report_text(taint._cell(ws))),
                             [0, 0, 0])

    def test_a_py_cell_reads_every_archived_arrangement_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d, arrangements=(0, 0, 1), top=self.LINE.format(n=1))
            self.assertEqual(taint.mount_counts_seen(taint.mount_report_text(taint._cell(ws))),
                             [0, 0, 1])
            self.assertFalse(taint.never_mounted(taint.mount_report_text(taint._cell(ws))))

    def test_a_py_cell_that_never_mounted_is_caught(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d, arrangements=(0, 0))
            self.assertTrue(taint.never_mounted(taint.mount_report_text(taint._cell(ws))))

    def test_runs_that_were_not_charged_are_not_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            for name, n in (("01-a1-BBS-charged", 1), ("02-a2-BBS-refunded", 0),
                            ("03-a2-BBS-interrupted", 0), ("04-a2-BBS-charged", 2)):
                a = ws / "arrangements" / name
                a.mkdir(parents=True)
                (a / "verify.log").write_text(self.LINE.format(n=n))
                (a / "deploy.log").write_text(name)
            self.assertEqual(taint.mount_counts_seen(taint.mount_report_text(taint._cell(ws))), [1, 2])
            self.assertEqual(taint._last_deploy_log(taint._cell(ws)), "04-a2-BBS-charged")

    def test_no_archive_falls_back_to_the_top_level_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = self._ws(d, top=self.LINE.format(n=3))
            self.assertEqual(taint.mount_counts_seen(taint.mount_report_text(taint._cell(ws))), [3])


class TestHostSleepGap(unittest.TestCase):
    """A suspend inside a load window leaves a wall-clock gap between sidecar
    ticks. The rule reads it off any surviving trace and warns; a green is
    never false, so it is a warn, not a taint."""

    def _trace(self, path, gaps):
        import pathlib
        p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        t, rows = 1000.0, ["epoch,ts_s"]
        for g in gaps:
            t += g
            rows.append(f"{t},{len(rows)}")
        p.write_text("\n".join(rows) + "\n")

    def test_ordinary_ticks_are_no_gap(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._trace(f"{d}/trace.csv", [1, 1, 1.2, 1])
            self.assertEqual(taint.host_sleep_gap_s(taint._cell(d)), 0)

    def test_a_suspend_in_an_archived_arrangement_is_found(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._trace(f"{d}/trace.csv", [1, 1, 1])
            self._trace(f"{d}/arrangements/02-BBS/trace.csv", [1, 899, 1])
            self.assertEqual(taint.host_sleep_gap_s(taint._cell(d)), 899)

    def test_the_rule_writes_the_field_and_warns(self):
        src = (Path(ROOT) / "experiment" / "taint.py").read_text()
        body = src[src.index("    slept = host_sleep_gap_s(cell)"):src.index("def selftest(")]
        self.assertIn('"host_sleep_gap_s": slept', body)
        self.assertIn("host-sleep gap", body)
        engine = (Path(ENGINE_TREE) / "fae" / "experiment" / "scoring" / "validate.py").read_text()
        self.assertIn("**fields,", engine)      # the experiment's fields land in validation.json
