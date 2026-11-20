"""The driver after the last bash port: exp1 (experiment/exp1.py), smoke/prepare
(the engine's Experiment, fae/experiment/), infra (fae/experiment/check_exp.py) and
the teardown paths reach fae/cell directly
— no shell driver, no shim.

What is pinned: the SHAPE of each verb (which fae/cell call it makes,
and that no `bash` sits between), and the behaviour a unit can reach with
the infra mocked out. The infra itself is proven by
`cli.py experiment smoke` on the host.
"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ENGINE_TREE, ROOT, at_workspace, runs
import fae.experiment

EXP1_SRC = (Path(ROOT) / "experiment" / "exp1.py").read_text()
CELL_SRC = (Path(ENGINE_TREE) / "fae" / "cell" / "cell.py").read_text()
SUPERVISE_SRC = (Path(ENGINE_TREE) / "fae" / "conduct" / "supervise.py").read_text()
CONDUCT_SRC = (Path(ENGINE_TREE) / "fae" / "conduct" / "__init__.py").read_text()
EXPERIMENT_SRC = (Path(ENGINE_TREE) / "fae" / "experiment" / "_experiment.py").read_text()
CHECK_SRC = (Path(ENGINE_TREE) / "fae" / "experiment" / "check_exp.py").read_text()


def body_of(name):
    """A function's source, or a method's when `name` is `Class.method`."""
    if "." in name:
        cls, meth = name.split(".")
        for src in (CONDUCT_SRC, CELL_SRC, SUPERVISE_SRC, EXPERIMENT_SRC):
            if f"\nclass {cls}" in src:
                break
        b = src[src.index(f"\n    def {meth}(self") + 1:]
        i = b.find("\n    def ", 1)
        return b if i < 0 else b[:i]
    for src in (EXP1_SRC, SUPERVISE_SRC, CONDUCT_SRC, CHECK_SRC):
        if f"\ndef {name}(" in src:
            break
    b = src[src.index(f"\ndef {name}(") + 1:]
    i = b.find("\ndef ", 1)
    return b if i < 0 else b[:i]


class TestExp1RunsTheVerifyInProcess(unittest.TestCase):
    """The reference benchmark builds the same Cell a scored run builds and
    calls its verify — one Verify, canonical shape, under the fleet's verify
    lock — instead of shelling to a verifier script."""

    def test_the_verify_is_a_method_call(self):
        b = body_of("_exp1_verify")
        for step in ("c.prepare(fresh=True)", "c.setup()", "with_verify_lock(c)",
                     "c.verify()", "c.teardown()"):
            self.assertIn(step, b, step)
        self.assertNotIn("subprocess", b)
        self.assertNotIn('"bash"', b)

    def test_the_lock_is_released_and_the_arm_torn_down_on_every_path(self):
        b = body_of("_exp1_verify")
        self.assertLess(b.index("with_verify_lock(c)"), b.index("c.verify()"))
        self.assertIn("c.teardown()", b[b.rindex("finally:"):])

    def test_exp1_calls_it_and_reads_the_same_files(self):
        b = body_of("exp1")
        self.assertIn("_exp1_verify(cid, arm, rep)", b)
        self.assertNotIn("subprocess", b)
        for f in ("metrics.json", "resources.json", "trace_events.json",
                  "service.deploy.log"):
            self.assertIn(f, b, f)

    def test_the_reference_cell_seeds_the_variants_known_answer(self):
        b = body_of("_reference_cell")
        self.assertIn("reference=True", b)
        self.assertIn("fae.experiment.exp().cell(", b)

    def test_a_reference_cell_is_a_cell(self):
        with tempfile.TemporaryDirectory() as d:
            c = runs.exp1._reference_cell("ref_high_smoke_kedap_sealed_apidocs_T1_r1",
                                          "kedap_sealed_apidocs", 1, Path(d))
        self.assertEqual((c.task, c.variant, c.reference, c.rep),
                         ("T1", "kedap_sealed_apidocs", True, 1))
        self.assertEqual(c.ws.parent, Path(d))


class TestExp1MountTotal(unittest.TestCase):
    """exp1's mount total runs from the first spike's own saturation: the
    canonical arrangement opens with two blips whose queues cross the
    saturation threshold a minute before the spike does."""

    def ws(self, d, hot, mounted):
        ws = Path(d)
        (ws / "schedule.json").write_text(json.dumps({"shape": "BBS", "events": [
            {"kind": "blip", "t0": 8.0, "t1": 30.0}, {"kind": "blip", "t0": 38.0, "t1": 60.0},
            {"kind": "spike", "t0": 68.0, "t1": 121.0}]}))
        (ws / "trace_events.json").write_text(json.dumps({"sat_p99_ms": 150.0}))
        lines = ["ts_s,p99_ms,cache_mounted,infra_cache_up"]
        for ts in range(130):
            up = any(a <= ts < b for a, b in mounted)
            p99 = 800.0 if any(a <= ts < b for a, b in hot) else 12.0
            lines.append(f"{ts}.0,{p99},{up},{up}")
        (ws / "trace.csv").write_text("\n".join(lines) + "\n")
        return ws

    def test_a_blips_saturation_is_not_the_spikes(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.ws(d, hot=[(15, 18), (80, 89)], mounted=[(89, 120)])
            self.assertEqual(runs.exp1.first_spike_mount_s(runs.Cell(ws.name, workspaces=ws.parent)), 9.0)

    def test_no_mount_in_the_spike_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            ws = self.ws(d, hot=[(15, 18), (80, 120)], mounted=[(18, 30)])
            self.assertIsNone(runs.exp1.first_spike_mount_s(runs.Cell(ws.name, workspaces=ws.parent)))


class TestLogEpoch(unittest.TestCase):
    def test_parses_the_leading_timestamp(self):
        ts = runs.exp1.log_epoch("2026-08-30 10:00:00,500 sustained saturation")
        self.assertAlmostEqual(ts, runs.exp1.datetime.strptime(
            "2026-08-30 10:00:00,500", "%Y-%m-%d %H:%M:%S,%f").timestamp())

    def test_a_line_with_no_timestamp_is_none(self):
        self.assertIsNone(runs.exp1.log_epoch("no timestamp here"))


class TestPulumiLogMarks(unittest.TestCase):
    def test_the_four_milestones_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "service.deploy.log"
            p.write_text(
                "2026-08-30 10:00:00,000 sustained saturation -> mounting cache\n"
                "2026-08-30 10:00:01,000 pulumi up: Updating (cache)\n"
                "2026-08-30 10:00:02,000 pulumi up: pulumi:pulumi:Stack proj-x created\n"
                "2026-08-30 10:00:03,000 cache mounted and answering\n")
            dec, up0, created, answer = runs.exp1.pulumi_log_marks(p)
        for v in (dec, up0, created, answer):
            self.assertIsNotNone(v)
        self.assertLess(dec, up0)
        self.assertLess(up0, created)
        self.assertLess(created, answer)

    def test_missing_file_is_four_nones(self):
        self.assertEqual(runs.exp1.pulumi_log_marks(Path("/no/such/log")),
                         (None, None, None, None))


class TestSpan(unittest.TestCase):
    def test_rounds_the_difference(self):
        self.assertEqual(runs.exp1.span(1.0, 3.456), 2.46)

    def test_either_side_none_is_none(self):
        self.assertIsNone(runs.exp1.span(None, 1.0))
        self.assertIsNone(runs.exp1.span(1.0, None))


class TestExp1VerifyTheRealSequence(unittest.TestCase):
    """_exp1_verify with fae.cell.Cell mocked out: proves the control
    flow (lock/teardown on every path), not the infra."""

    def _fake_cell(self, setup_rc=0, lock=mock.DEFAULT, verify_result="RESULT",
                   verify_raises=None):
        c = mock.Mock()
        c.setup.return_value = (setup_rc, "")
        if lock is mock.DEFAULT:
            lock = mock.Mock()
        c.verify_lock_acquire.return_value = lock
        if verify_raises is not None:
            c.verify.side_effect = verify_raises
        else:
            c.verify.return_value = verify_result
        return c

    def test_the_happy_path_returns_verify_and_releases_everything(self):
        c = self._fake_cell()
        with mock.patch.object(runs.exp1, "_reference_cell", return_value=c):
            result = runs.exp1._exp1_verify("cid", "pulumi_sealed", 1)
        self.assertEqual(result, "RESULT")
        c.prepare.assert_called_once_with(fresh=True)
        c.verify_lock_acquire.return_value.close.assert_called_once()
        c.teardown.assert_called_once()

    def test_a_setup_failure_tears_down_and_raises_without_verifying(self):
        c = self._fake_cell(setup_rc=1)
        with mock.patch.object(runs.exp1, "_reference_cell", return_value=c):
            with self.assertRaises(RuntimeError):
                runs.exp1._exp1_verify("cid", "pulumi_sealed", 1)
        c.verify.assert_not_called()
        c.teardown.assert_called_once()

    def test_a_stood_down_lock_raises_and_still_tears_down(self):
        c = self._fake_cell(lock=None)
        with mock.patch.object(runs.exp1, "_reference_cell", return_value=c):
            with self.assertRaises(RuntimeError):
                runs.exp1._exp1_verify("cid", "pulumi_sealed", 1)
        c.verify.assert_not_called()
        c.teardown.assert_called_once()

    def test_a_verify_exception_still_releases_lock_and_tears_down(self):
        c = self._fake_cell(verify_raises=ValueError("boom"))
        with mock.patch.object(runs.exp1, "_reference_cell", return_value=c):
            with self.assertRaises(ValueError):
                runs.exp1._exp1_verify("cid", "pulumi_sealed", 1)
        c.verify_lock_acquire.return_value.close.assert_called_once()
        c.teardown.assert_called_once()


class TestExp1AggregatesAndWritesTheReport(unittest.TestCase):
    """exp1(args) with _exp1_verify mocked out (report_only exercises the
    same aggregation without even that): proves the read/aggregate/write
    path against real files on disk, not just its shape."""

    def _write_green_run(self, ws):
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "metrics.json").write_text(json.dumps(
            {"scaling_ok": True, "e2e_green": True, "load_errors": 0}))
        (ws / "resources.json").write_text(json.dumps({
            "peak": {"total_mem_mb": 100.0, "total_cpu_pct": 50.0,
                      "component_count": 3, "app_mem_mb": 40.0,
                      "app_cpu_pct": 20.0, "by_group": {}},
            "avg_cores": 1.5}))
        (ws / "trace_events.json").write_text(json.dumps(
            {"mount_epoch": 12.0, "ceiling_qps": 10, "decision_epoch": 10.0}))
        (ws / "trace.csv").write_text("epoch,offered_rps\n8.0,5\n9.0,11\n")
        (ws / "service.deploy.log").write_text("")

    def test_a_green_report_only_run_writes_the_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            smoke_ws = root / "ws-smoke.nosync"
            cid = "ref_high_smoke_pulumi_sealed_apidocs_T1_r1"
            self._write_green_run(smoke_ws / cid)
            out = io.StringIO()
            with at_workspace(root=root), \
                    contextlib.redirect_stdout(out):
                runs.exp1.exp1(runs.argparse.Namespace(
                    variants="pulumi_sealed_apidocs", reps=1, report_only=True))
            written = root / "results" / f"exp1-{runs.exp1.time.strftime('%Y%m%d')}.md"
            self.assertTrue(written.is_file())
            self.assertIn("| pulumi_sealed_apidocs | 1/1 |", written.read_text())
            self.assertIn("GREEN", out.getvalue())

    def test_missing_metrics_is_no_data_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            smoke_ws = root / "ws-smoke.nosync"
            out = io.StringIO()
            with at_workspace(root=root), \
                    contextlib.redirect_stdout(out):
                runs.exp1.exp1(runs.argparse.Namespace(
                    variants="pulumi_sealed_apidocs", reps=1, report_only=True))
            self.assertIn("NO DATA", out.getvalue())
            self.assertIn("| pulumi_sealed_apidocs | 0/0 |",
                         (root / "results" / f"exp1-{runs.exp1.time.strftime('%Y%m%d')}.md").read_text())

    def test_a_verify_prep_failure_is_reported_and_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            smoke_ws = root / "ws-smoke.nosync"
            out = io.StringIO()
            with at_workspace(root=root), \
                    mock.patch.object(runs.exp1, "_exp1_verify",
                                      side_effect=RuntimeError("setup failed")), \
                    contextlib.redirect_stdout(out):
                runs.exp1.exp1(runs.argparse.Namespace(
                    variants="pulumi_sealed_apidocs", reps=1, report_only=False))
            self.assertIn("PREP FAIL", out.getvalue())


class TestExp1ParsesItsOwnArguments(unittest.TestCase):
    """`cli.py experiment verb exp1 ARGS`: the engine passes argv through, and
    exp1.main owns the options and their defaults."""

    def _parsed(self, argv):
        with mock.patch.object(runs.exp1, "exp1") as m:
            runs.exp1.main(argv)
        (ns,), _ = m.call_args
        return ns

    def test_the_defaults_are_three_reps_of_the_sealed_trio(self):
        ns = self._parsed([])
        self.assertEqual((ns.reps, ns.variants, ns.report_only),
                         (3, ",".join(runs.exp1.EXP1_VARIANTS), False))

    def test_explicit_options_pass_through(self):
        ns = self._parsed(["--variants", "kedap_sealed_apidocs", "--reps", "2", "--report-only"])
        self.assertEqual((ns.reps, ns.variants, ns.report_only), (2, "kedap_sealed_apidocs", True))

    def test_it_is_the_definitions_exp1_command(self):
        self.assertIs(runs.experiment.exp().definition.commands["exp1"], runs.exp1.main)


class TestSmokeIsAStubCellRun(unittest.TestCase):
    """One reference cell per arm through `-m fae.cell ... --stub`,
    prepared fresh in ws-smoke.nosync, exit 0 iff every arm is green."""

    def test_the_shape(self):
        b = body_of("Experiment.smoke_cells")
        self.assertIn('verbs["reference_cell"](', b)
        self.assertIn("prepare(fresh=True)", b)
        self.assertIn("c.process_argv()", b)
        self.assertIn('"--stub"', b)
        self.assertNotIn("smoke.sh", b)
        self.assertEqual(runs.experiment._experiment.Experiment.SMOKE_DIR, "ws-smoke.nosync")

    def test_the_stub_cell_is_a_ref_smoke_cell_outside_the_scored_tree(self):
        b = body_of("Experiment.smoke_cells")
        for kv in ('WORKSPACES_DIR=str(smoke_ws)', 'AGENT="ref"',
                   'SMOKE="1"', 'REFERENCE="1"'):
            self.assertIn(kv, b, kv)

    def _run(self, rc_by_arm, metrics_by_arm, argv):
        calls = []

        class _FakeSlots:
            def handover(self):
                return ""

            def fds(self):
                return []

            def close(self):
                pass

        class FakeCell:
            def __init__(self, ws, vid, rep):
                self.ws, self.vid, self.rep = ws, vid, rep
                self.cid = ws.name

            def process_argv(self):
                return ["python3", "-m", "fae.cell", "T1", self.vid, str(self.rep)]

            def prepare(self, fresh=False):
                self.ws.mkdir(parents=True, exist_ok=True)

            def ready_image(self, log=print):
                return True

            def take_slots(self):
                return _FakeSlots(), None

        def fake_reference_cell(cid, vid, rep, workspaces):
            return FakeCell(workspaces / cid, vid, rep)

        def fake_run(cmd, cwd=None, env=None, **kw):
            if cmd[0] == "docker":                     # the tooling preflight
                return mock.Mock(returncode=0)
            arm = cmd[cmd.index("fae.cell") + 2]      # the variant
            calls.append((arm, env.get("AGENT"), env.get("SMOKE"),
                          env.get("WORKSPACES_DIR"), env.get("SHAPE_GATE")))
            ws = Path(env["WORKSPACES_DIR"]) / runs.cell_id("ref", arm, 1, "T1", smoke=True)
            (ws / "metrics.json").write_text(json.dumps(metrics_by_arm[arm]))
            return mock.Mock(returncode=rc_by_arm[arm])

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runs.experiment._experiment.Experiment, "smoke_workspaces",
                                  new_callable=mock.PropertyMock, return_value=Path(d)), \
                mock.patch.object(runs.exp1, "_reference_cell", fake_reference_cell), \
                mock.patch.object(runs.subprocess, "run", fake_run), \
                mock.patch.object(runs.experiment._experiment, "_run_to_teardown", fake_run), \
                mock.patch.object(runs.check, "probe_variants", return_value=0), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with self.assertRaises(SystemExit) as cm:
                runs.experiment.exp().smoke(**argv)
        return cm.exception.code, calls, out.getvalue()

    GREEN = {"e2e_green": True, "e2e_pass": 7, "e2e_total": 7,
             "k6_available": True, "scaling_ok": True}

    def test_every_arm_green_exits_0(self):
        code, calls, out = self._run(
            {"pulumi_sealed_apidocs": 0, "kedap_sealed_apidocs": 0},
            {"pulumi_sealed_apidocs": self.GREEN, "kedap_sealed_apidocs": self.GREEN},
            dict(variants="pulumi_sealed_apidocs,kedap_sealed_apidocs", only="", rep=1, full_gate=False))
        self.assertEqual(code, 0, out)
        self.assertEqual([c[0] for c in calls], ["pulumi_sealed_apidocs", "kedap_sealed_apidocs"])
        arm, model, smoke, ws, gate = calls[0]
        self.assertEqual((model, smoke, gate), ("ref", "1", "one"))
        self.assertIn("PIPELINE OK", out)

    def test_one_failing_arm_exits_1_and_names_the_log(self):
        code, _, out = self._run(
            {"pulumi_sealed_apidocs": 0, "kedap_sealed_apidocs": 1},
            {"pulumi_sealed_apidocs": self.GREEN,
             "kedap_sealed_apidocs": {"e2e_green": True, "e2e_pass": 7, "e2e_total": 7,
                             "k6_available": True, "scaling_ok": False,
                             "scaling_why": "saw 0"}},
            dict(variants="pulumi_sealed_apidocs,kedap_sealed_apidocs", only="", rep=1, full_gate=False))
        self.assertEqual(code, 1)
        self.assertIn("SCALING FAILED", out)
        self.assertIn("verify.log", out)
        self.assertIn("PIPELINE BROKE", out)

    def test_a_driver_that_exits_non_zero_is_not_green_even_with_green_metrics(self):
        code, _, out = self._run(
            {"pulumi_sealed_apidocs": 45}, {"pulumi_sealed_apidocs": self.GREEN},
            dict(variants="pulumi_sealed_apidocs", only="", rep=1, full_gate=False))
        self.assertEqual(code, 1)
        self.assertIn("driver rc=45", out)

    def test_full_gate_leaves_the_gate_setting_to_config(self):
        _, calls, _ = self._run(
            {"pulumi_sealed_apidocs": 0}, {"pulumi_sealed_apidocs": self.GREEN},
            dict(variants="pulumi_sealed_apidocs", only="", rep=1, full_gate=True))
        self.assertIsNone(calls[0][4])

    def test_only_filters_by_substring(self):
        _, calls, _ = self._run(
            {"kedap_access_apidocs": 0}, {"kedap_access_apidocs": self.GREEN},
            dict(variants="", only="kedap_acc", rep=1, full_gate=False))
        self.assertEqual([c[0] for c in calls], ["kedap_access_apidocs"])

    def test_classify_localizes_the_break(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertEqual(_verdict(ws)[0], False)
            self.assertIn("NO METRICS", _verdict(ws)[1])
            (ws / "metrics.json").write_text(json.dumps(
                {"e2e_green": False, "stage_failed": "deploy", "deploy_ok": False}))
            green, why = _verdict(ws)
            self.assertFalse(green)
            self.assertIn("deploy.log", why)
            (ws / "metrics.json").write_text(json.dumps(
                {"e2e_green": True, "e2e_pass": 7, "e2e_total": 7,
                 "k6_available": False}))
            green, why = _verdict(ws)
            self.assertTrue(green)
            self.assertIn("K6 MISSING", why)


def _verdict(ws):
    return runs.experiment._experiment.Experiment.smoke_verdict(runs.experiment.exp().cell(ws.name, workspaces=ws.parent))


class TestPrepareIsTheDriversPrepare(unittest.TestCase):

    def test_it_seeds_every_active_variant_through_the_module(self):
        seen = []
        fake = mock.Mock(side_effect=lambda cid, *a, **kw: seen.append((cid, kw)) or Path("/x") / cid)
        import fae.cell.prepare as prep
        locks = tempfile.TemporaryDirectory()
        self.addCleanup(locks.cleanup)
        with mock.patch.object(prep, "prepare", fake), \
                at_workspace(plane=Path(locks.name)), \
                contextlib.redirect_stdout(io.StringIO()):
            runs.experiment.exp().prepare("sonnet", 2, "T1")
        active = runs.experiment.exp().definition.active
        self.assertEqual(len(seen), len(active) * 2)
        cid, kw = seen[0]
        self.assertEqual(cid, runs.cell_id("sonnet", active[0], 1))
        self.assertEqual(kw["impl"], "fae")
        self.assertEqual(kw["workspaces"], runs.experiment.exp().workspace.path)

    def test_a_missing_agent_is_refused(self):
        with mock.patch.dict(os.environ, {"AGENT": ""}), \
                self.assertRaises(SystemExit):
            runs.cli.experiment_prepare(agent="", reps=1, task="T1")


class TestInfraIsThePreflight(unittest.TestCase):

    def test_every_active_variant_is_asked_and_stale_clusters_swept(self):
        import fae.experiment.variants as tr
        from fae.cell.substrate import K8sSubstrate
        asked = []

        class T:
            def __init__(self, vid, cid, ws, root, conf=None, *, definition=None):
                self.vid = vid
                self.variant = runs.experiment.exp().definition.variant(vid)

            def ok(self):
                asked.append(self.vid)
                return self.vid != "kedap_access_apidocs"

            def image(self):
                return "fae-ecb-x:0"

        out = io.StringIO()
        with mock.patch.object(tr, "infra_for", T), \
                mock.patch.object(K8sSubstrate, "sweep") as sweep, \
                contextlib.redirect_stdout(out):
            bad = runs.check.infra()
        self.assertEqual(sorted(asked), sorted(runs.experiment.exp().definition.active))
        self.assertTrue(sweep.called)
        self.assertEqual(bad, 1)
        self.assertIn("[HALT] kedap_access_apidocs", out.getvalue())

    def test_it_creates_no_shared_infra(self):
        b = body_of("infra")
        self.assertNotIn("ensure_infra", b)
        self.assertNotIn("kind create", b)


class TestTeardownIsInProcess(unittest.TestCase):
    """stop, conduct's kill fallback and the zombie reaper tear an arm down
    through the variants (experiment/variants/) — the driver's own teardown — not
    through a shell hook."""

    def test_every_teardown_site_calls_the_one_function(self):
        # the run ends a cell through Cell.take_down, which tears the infra
        # down after the loop is gone; an orphan's teardown is the cell's own;
        # a stop tears down through Cell.kill / Cell.clean_up
        self.assertIn("self.teardown_within(", body_of("Cell.take_down"))
        self.assertIn(".teardown()", body_of("supervise_pass"))
        self.assertIn(".take_down(", body_of("supervise_pass"))
        self.assertIn("self.clean_up(", body_of("Cell.stop"))
        for f in sorted((Path(ENGINE_TREE) / "fae").rglob("*.py")):
            src = f.read_text()
            self.assertNotIn("treatment_teardown \"$1\"", src, f.name)
            self.assertNotIn("lib.sh; load_config", src, f.name)

    def test_a_named_cell_carries_its_variant_where_its_workspace_does_not(self):
        c = runs.experiment.exp().workspace.named_cell("x_high_kedap_access_apidocs_T1_r1")
        self.assertEqual(c.variant, "kedap_access_apidocs")
        self.assertEqual(runs.experiment.exp().workspace.named_cell("x", "kedap_access_apidocs").variant,
                         "kedap_access_apidocs")

    def test_no_variant_is_a_no_op(self):
        from fae.cell.cell import Cell
        with mock.patch.object(Cell, "teardown") as td, \
                mock.patch.object(Cell, "loop_pid", return_value=None), \
                mock.patch.object(runs.subprocess, "run"):
            runs.experiment.exp().workspace.named_cell("x").take_down("stopped")
        self.assertFalse(td.called)

    def test_a_hung_teardown_is_abandoned_after_the_timeout_and_reported(self):
        import threading
        from fae.cell.cell import Cell
        release = threading.Event()
        logged = []
        with mock.patch.object(Cell, "teardown", lambda self: release.wait(5)), \
                mock.patch.object(Cell, "TEARDOWN_TIMEOUT_S", 0.2), \
                mock.patch.object(Cell, "loop_pid", return_value=None), \
                mock.patch.object(runs.subprocess, "run"):
            runs.experiment.exp().workspace.named_cell("x_high_pulumi_access_apidocs_T1_r1").take_down(
                "stopped", log=logged.append)
        release.set()
        self.assertTrue(any("abandoned" in l for l in logged), logged)

    def test_the_conduct_kill_path_bounds_the_wait(self):
        self.assertIn("self.teardown_within(self.TEARDOWN_TIMEOUT_S)",
                      body_of("Cell.take_down"))


class TestTheDrainWaitsForOutOfLoopVerifies(unittest.TestCase):

    def test_the_drain_names_the_same_holders_as_the_reaper(self):
        b = body_of("Conduct.pause")
        self.assertIn("re.search(zombies.VERIFY_HOLDER_ARGV, l)", b)
        self.assertNotIn("run_exp_artifact", b)

    def test_the_stranded_reverify_check_names_the_same_holders_too(self):
        """A second, undocumented hardcoded copy of the holder pattern
        (`r"runs\\.py\\s+reverify\\b"`) lived here from Milestone 2's
        supervise.py extraction until Milestone 5 folded it — one shared
        pattern, or the two drift the moment cli.py's invocation shape is
        added to only one of them."""
        b = body_of("supervise_pass")
        self.assertIn("re.search(zombies.VERIFY_HOLDER_ARGV, l)", b)
        self.assertNotIn('"runs\\.py\\s+reverify', b)


class TestNothingShellsToTheRetiredScripts(unittest.TestCase):

    def test_driver_names_none_of_them(self):
        for f in sorted((Path(ENGINE_TREE) / "fae").rglob("*.py")):
            src = f.read_text()
            for name in ("run_exp_artifact", "prepare_cell.sh", "smoke.sh",
                         "ensure_infra", "cell_setup.sh", "cell_teardown.sh",
                         "_work_slots", "_arm_lock", "_mutex.sh", "_transitions.sh"):
                self.assertNotIn(name, src, f"{f.name}: {name}")

    def test_cli_py_names_none_of_them(self):
        src = (Path(ENGINE_TREE) / "fae" / "cli" / "__init__.py").read_text()
        for name in ("prepare_cell.sh", "smoke.sh", "ensure_infra",
                     "_cell_id_cmd", "_seed_doc_cmd", 'runs._cell_id', 'runs._seed_doc'):
            self.assertNotIn(name, src, name)


if __name__ == "__main__":
    unittest.main()
