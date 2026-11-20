"""The ported verify: the parts that decide a verdict, without the infra.

run_exp_artifact.sh carried 34 mutable globals and shipped them out as
environment variables at the end. The risk of porting it is not that the docker
choreography changes — that still runs the same shell — but that a CLASSIFICATION
changes: which failures are scored against the agent, which are refunded as rig
faults, and what "green" means. Those are what is tested here.

The classification goldens were first pinned against the bash driver this
replaced (run_exp_artifact.sh, retired 2026-08-29) and stay pinned as the
numbers and names every recorded cell was judged by.
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _ctx import ENGINE_TREE, ROOT

ROOT = Path(ROOT)
VERIFY_SRC = (ROOT / "experiment" / "verifier" / "__init__.py").read_text()
CELL_SRC = (ENGINE_TREE / "fae" / "cell" / "cell.py").read_text()
ENGINE_SRC = (ENGINE_TREE / "fae" / "cell" / "verify.py").read_text()

from experiment import verifier as verify
import fae.experiment
from fae.cell import Cell, Verdict


class VerifyTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.art = self.d / "artifacts"
        self.out = self.d / "ws"
        self.art.mkdir()
        self.out.mkdir()

    def v(self, task="T1", variant="pulumi_sealed_apidocs", shape=None, cfg=None):
        return verify.Verify(self.art, self.out, task=task, variant=variant,
                             cid="cell-x", shape=shape, root=ROOT,
                             config=cfg if cfg is not None else {})

    def persist(self, v, files=()):
        """What the engine does with the verifier's answer (Cell._persist_verdict)."""
        c = Cell("cell-x", workspaces=self.d, root=ROOT)
        c._env.update({"TASK": v.task, "VARIANT": v.variant_id})
        c._persist_verdict(v.out, Verdict(ok=v.result.green, stage=v.result.stage_failed,
                                          metrics=v.result.to_metrics(),
                                          arrangement=v.arrangement_label(), files=tuple(files)))
        arr = v.out / "arrangements"
        return sorted(arr.iterdir())[-1] if arr.is_dir() else None


class TestMetricsJson(VerifyTestCase):
    """metrics.json is the engine's write of the verifier's metrics
    (Result.to_metrics): every field, null for "never determined", identity
    first."""

    def test_the_document_carries_every_field_with_null_semantics(self):
        v = self.v()
        r = v.result
        r.deploy_ok, r.e2e_pass, r.e2e_total, r.e2e_green = True, 7, 7, True
        r.load_ran, r.load_total, r.load_errors = True, 8048, 0
        r.scaling_ok, r.scaling_why, r.ceiling_qps = True, "", 53
        self.persist(v)
        doc = json.loads((self.out / "metrics.json").read_text())
        self.assertEqual(list(doc)[:3], ["cell_id", "task", "variant"])
        self.assertEqual(doc["cell_id"], "cell-x")
        self.assertEqual(doc["e2e_pass"], 7)
        self.assertIsNone(doc["k6_available"])          # never asked
        self.assertIsNone(doc["mount_delay_s"])         # never mounted
        self.assertIsNone(doc["scaling_why"])           # empty -> null
        self.assertIsNone(doc["stage_failed"])
        self.assertEqual(doc["ceiling_qps"], 53)
        self.assertTrue(doc["scaling_ok"])

    def test_a_stage_failure_is_recorded_as_such(self):
        v = self.v()
        v.result.stage_failed, v.result.scaling_ok = "store", None
        self.persist(v)
        doc = json.loads((self.out / "metrics.json").read_text())
        self.assertEqual(doc["stage_failed"], "store")
        self.assertIsNone(doc["scaling_ok"])
        self.assertFalse(doc["e2e_green"])


class TestTheExitRule(VerifyTestCase):
    """GREEN = e2e AND the scaling gate. Not e2e alone — that was the whole
    point of the gate — and not a scaling verdict that never ran."""

    def green(self, e2e, scaling):
        v = self.v()
        v.result.e2e_green = e2e
        v.result.scaling_ok = scaling
        return bool(v.result.e2e_green) and v.result.scaling_ok is not False

    def test_e2e_and_scaling_is_green(self):
        self.assertTrue(self.green(True, True))

    def test_a_scaling_failure_is_not_green_however_good_the_e2e(self):
        self.assertFalse(self.green(True, False))

    def test_a_failed_e2e_is_not_green(self):
        self.assertFalse(self.green(False, True))

    def test_a_gate_that_never_ran_does_not_fail_the_run(self):
        # scaling_ok is None when k6 is absent. `not false`, not `is true` —
        # the recorded rule was `[[ "$scaling_ok" != false ]]`.
        self.assertTrue(self.green(True, None))

    def test_the_rule_is_the_one_line_in_run(self):
        self.assertIn("r.green = bool(r.e2e_green) and r.scaling_ok is not False",
                      VERIFY_SRC)

    def test_a_spike_that_never_saturated_the_store_is_a_load_shape_void(self):
        # the law answers "void|…": nothing the policy could have seen, so
        # nothing to judge — refunded, and the ledger says why
        v = self.v()
        v.result.load_total, v.result.load_errors = "6000", "0"
        (v.out / "iterations.log").write_text("")
        why = "spike 1 never saturated the store (p99 max 14ms < 150ms over [8..61]) — raise [load] top"
        with mock.patch.object(v, "block", return_value=(0, "void|" + why)):
            v.scaling_gate(v.out / "trace.csv", v.out / "trace_events.json")
        self.assertEqual(v.result.stage_failed, "load-shape")
        self.assertIn("load-shape", verify.VOID_STAGES)
        self.assertFalse(v.result.scaling_ok)
        self.assertEqual(v.result.scaling_why, why)
        from fae.cell.verify import EVENTS
        self.assertIn("\tALERT\tLOAD-SHAPE " + why, (v.out / EVENTS).read_text())
        self.assertIn("FAIL[load-shape]", (v.out / "verify.log").read_text())

    def test_the_law_receives_the_load_keys(self):
        v = self.v(cfg={"LOAD_SAT_P99_MS": "200", "LOAD_MAX_MOUNT_S": "45"})
        v.result.load_total, v.result.load_errors = "6000", "0"
        with mock.patch.object(v, "block", return_value=(0, "true|")) as blk:
            v.scaling_gate(v.out / "trace.csv", v.out / "trace_events.json")
        env = blk.call_args.kwargs["env"]
        self.assertEqual(env["VERIFY_SAT_P99_MS"], "200.0")
        self.assertEqual(env["VERIFY_MAX_MOUNT_S"], "45.0")


class TestFailureClassification(VerifyTestCase):
    """Which stage a failure is blamed on decides whether an attempt is BURNED
    or REFUNDED. Getting it wrong costs either the agent's budget or the
    experiment's honesty."""

    def test_a_deploy_that_never_got_the_infra_up_is_refunded(self):
        # `nostart` -> rig fault, attempt not consumed, retried.
        v = self.v()
        with mock.patch.object(verify.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.assertEqual(v.deploy("pulumi"), "nostart")

    def test_a_deploy_rejected_AFTER_the_infra_is_scored(self):
        # What the bring-up raises separates a broken manifest (the agent's
        # fault, scored) from a rig that never came up. Without the split, a
        # broken manifest was refunded forever — and the refund erased the
        # failure from the agent's history, so it reproduced the same error
        # every cycle.
        v = self.v()
        with mock.patch.object(v, "infra", return_value=mock.Mock(
                TAG="pulumi", verify_setup=mock.Mock(side_effect=verify.service.Rejected("apply failed")))), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"):
            self.assertEqual(v.deploy("pulumi"), "rejected")
        self.assertIn("apply failed", (v.out / "deploy.log").read_text())

    def test_a_infra_that_never_came_up_is_a_nostart(self):
        v = self.v()
        with mock.patch.object(v, "infra", return_value=mock.Mock(
                TAG="pulumi", verify_setup=mock.Mock(side_effect=verify.HookFailure("dind absent")))), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"):
            self.assertEqual(v.deploy("pulumi"), "nostart")

    def test_a_service_that_never_answers_is_scored_with_the_infras_diagnostics(self):
        v = self.v()
        infra = mock.Mock(TAG="pulumi", verify_setup=mock.Mock(return_value=verify.service.Up()))
        with mock.patch.object(v, "infra", return_value=infra), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"), \
                mock.patch.object(verify.service.Bringup, "start_service"), \
                mock.patch.object(verify.service.Bringup, "service_ready",
                                  side_effect=verify.service.Rejected("not ready")):
            self.assertEqual(v.deploy("pulumi"), "rejected")
        infra.not_ready.assert_called_once()

    def test_the_service_starts_from_the_variants_run_after_the_infra(self):
        v = self.v()
        order = []
        up = verify.service.Up(env={"SERVICE_URL": "http://svc:1"})
        infra = mock.Mock(TAG="pulumi", variant="the-variant",
                          verify_setup=mock.Mock(side_effect=lambda c, e: order.append("setup") or up))
        with mock.patch.object(v, "infra", return_value=infra), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name",
                           side_effect=lambda *a, **k: order.append("clear")), \
                mock.patch.object(verify.service.Bringup, "fresh_copy",
                                  side_effect=lambda: order.append("copy")), \
                mock.patch.object(verify.service.Bringup, "start_service",
                                  side_effect=lambda variant, u: order.append(("start", variant, u))), \
                mock.patch.object(verify.service.Bringup, "service_ready",
                                  side_effect=lambda: order.append("ready")):
            self.assertEqual(v.deploy("pulumi"), "ok")
        self.assertEqual(order, ["clear", "copy", "setup", ("start", "the-variant", up), "ready"])

    def test_the_teardown_stops_the_service_before_the_infras_teardown(self):
        v = self.v()
        order = []
        infra = mock.Mock(TAG="pulumi", verify_teardown=lambda c, e: order.append("teardown"))
        with mock.patch.object(v, "infra", return_value=infra), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name",
                           side_effect=lambda cid, w, log=None: order.append(("stop", log.name))), \
                mock.patch.object(verify.rig, "db_down"), \
                mock.patch.object(verify.rig, "assert_no_cache_backend", return_value=None), \
                mock.patch.object(verify.contract, "for_variant",
                                  return_value=mock.Mock(after_teardown=lambda: [])):
            v.teardown("pulumi")
        self.assertEqual(order[:2], [("stop", "service.deploy.log"), "teardown"])

    # the verifier's own vocabulary (every sealed cell's metrics.json carries
    # one) plus the engine's: its gate, its fingerprint pin, its lock and the
    # verifier process itself
    STAGES = {"nostart", "deploy", "scaling", "e2e", "precheck",
              "store", "k6", "contract", "load-shape"}
    ENGINE_STAGES = {"harness-fp", "harness-heal", "exclusive-lock", "verifier",
                     "verifier-timeout", "verifier-image", "verifier-host", "infra", "integrity",
                     "rig-output", "host-sleep", "host-busy"}

    def test_the_stage_names_are_the_recorded_vocabulary(self):
        # fae/experiment/scoring/validate.py and score_cell both branch on these exact
        # strings, and every sealed cell's metrics.json carries one.
        written = set(re.findall(r'stage_failed ?= ?"([a-z0-9-]+)"', VERIFY_SRC))
        self.assertEqual(written, self.STAGES)
        engine = set(re.findall(r'stage(?:_failed)? ?= ?"([a-z0-9-]+)"', CELL_SRC + ENGINE_SRC))
        self.assertEqual(engine, self.ENGINE_STAGES)

    def test_every_void_stage_is_one_the_verify_can_produce(self):
        self.assertTrue(verify.VOID_STAGES <= self.STAGES,
                        verify.VOID_STAGES - self.STAGES)

    def test_a_missing_bringup_is_a_deploy_failure_not_a_crash(self):
        v = self.v(variant="nosuchtech")
        self.assertEqual(v.deploy("nosuchtech"), "missing")


class TestTheK6CountsAreNumbers(unittest.TestCase):
    """metrics.json carries the load counts as numbers: the taint rules divide
    them, and the results table counts a cell's load errors only when
    load_total is an int."""

    def test_a_full_line(self):
        self.assertEqual(verify.Verify.k6_counts("28114 0 0 0 53.0"), (28114, 0, 53.0))

    def test_a_short_line_has_no_mount_delay(self):
        self.assertEqual(verify.Verify.k6_counts("8049 12"), (8049, 12, None))

    def test_a_field_that_is_not_a_number_is_absent(self):
        self.assertEqual(verify.Verify.k6_counts("n/a 3 0 0 x"), (None, 3, None))
        self.assertEqual(verify.Verify.k6_counts(""), (None, None, None))

    def test_run_load_takes_its_counts_from_it(self):
        src = (Path(ROOT) / "experiment" / "verifier" / "__init__.py").read_text()
        body = src[src.index("    def run_load(self"):]
        self.assertIn("self.k6_counts(mout)", body[:body.index("\n    def ")])


class TestTheE2EContract(VerifyTestCase):
    """Same assertions, same count, same task split. The count matters: it is
    recorded as e2e_pass/e2e_total and read back by the scorer."""

    def responses(self, **overrides):
        base = {
            ("GET", "/health"): (200, '{"status": "ok", "cache_mounted": false}'),
            ("GET", "/metrics"): (200, "pool_pressure 0.1"),
            ("POST", "/photos"): (201, '{"id": "00000001"}'),
            ("GET", "/photos/00000001"): (200, '{"id": "00000001", "owner": "e2e", "caption": "TOK sunset over the beach"}'),
            ("GET", "/photos?owner=e2e"): (200, '{"photos": ["00000001"]}'),
            ("GET", "/photos/search?q=sunset"): (200, '{"photos": []}'),
        }
        base.update(overrides)
        return base

    def run_e2e(self, task, responses, token_echo=True):
        v = self.v(task=task)
        seen = []

        def http(method, path, body=None, timeout=10):
            seen.append((method, path))
            code, text = responses.get((method, path), (404, ""))
            if token_echo and path.startswith("/photos/0") and "TOK" in text:
                text = text.replace("TOK", self._tok)
            return code, text

        # capture the token the assertion generates, so the round-trip check
        # can be satisfied the way a real service satisfies it
        real_http = v.http
        self._tok = ""

        def capture(method, path, body=None, timeout=10):
            if method == "POST" and body and "caption" in body:
                self._tok = body["caption"].split()[0]
            return http(method, path, body, timeout)

        v.http = capture
        green = v.e2e()
        return v, green, seen

    def test_T1_asserts_seven_things(self):
        v, green, _ = self.run_e2e("T1", self.responses())
        self.assertEqual((v.result.e2e_pass, v.result.e2e_total), (7, 7))
        self.assertTrue(green)

    def test_an_unknown_task_fails_rather_than_passing_vacuously(self):
        v, green, _ = self.run_e2e("T9", self.responses())
        self.assertFalse(green)
        self.assertEqual(v.result.e2e_pass, 3)      # the three common checks
        self.assertEqual(v.result.e2e_total, 4)

    def test_cache_mounted_must_be_PRESENT_in_health(self):
        # The contract requires a truthful cache_mounted field; a /health
        # without one is a failure, not a pass by omission.
        r = self.responses()
        r[("GET", "/health")] = (200, '{"status": "ok"}')
        v, green, _ = self.run_e2e("T1", r)
        self.assertFalse(green)

    def test_metrics_must_export_pool_pressure(self):
        r = self.responses()
        r[("GET", "/metrics")] = (200, "something_else 1")
        v, green, _ = self.run_e2e("T1", r)
        self.assertFalse(green)

    def test_a_missing_id_does_not_skip_the_round_trip_silently(self):
        # bash counts the skipped assertion as a FAILURE (`bad "... skipped"`),
        # so the total stays 7 and the cell is not quietly credited.
        r = self.responses()
        r[("POST", "/photos")] = (500, "")
        v, green, _ = self.run_e2e("T1", r)
        self.assertEqual(v.result.e2e_total, 7)
        self.assertFalse(green)


class TestOutDirIsWhatMakesReverifyNonDestructive(VerifyTestCase):

    def test_every_arrangement_file_is_written_under_out_dir(self):
        v = self.v()
        v.prepare_out()
        for f in ("verify.log", "k6.log", "deploy.log", "teardown.log"):
            self.assertTrue((self.out / f).exists())

    def test_preparing_the_out_dir_does_not_touch_the_artifacts(self):
        (self.art / "app.py").write_text("x = 1\n")
        before = sorted(p.name for p in self.art.iterdir())
        v = self.v()
        v.prepare_out()
        v.say("hello")
        self.assertEqual(sorted(p.name for p in self.art.iterdir()), before)

    def test_a_separate_out_dir_leaves_the_recorded_result_alone(self):
        # The thing the bash driver could not do: WORKSPACES_DIR is computed
        # unconditionally there, so out and ws were always the same directory.
        recorded = self.d / "recorded"
        recorded.mkdir()
        (recorded / "metrics.json").write_text('{"e2e_pass": 7}')
        (recorded / "verify.log").write_text("the original run\n")
        v = verify.Verify(self.art, self.d / "reverify-1", task="T1",
                          variant="pulumi_sealed_apidocs", root=ROOT, config={})
        v.prepare_out()
        v.say("a second opinion")
        self.assertEqual((recorded / "verify.log").read_text(), "the original run\n")
        self.assertEqual(json.loads((recorded / "metrics.json").read_text()),
                         {"e2e_pass": 7})

    def test_arrangements_are_numbered_not_overwritten(self):
        v = self.v(shape="BBS")
        v.prepare_out()
        first = self.persist(v, verify.ARRANGEMENT_FILES)
        second = self.persist(v, verify.ARRANGEMENT_FILES)
        self.assertNotEqual(first, second)
        self.assertRegex(first.name, r"^01-a\d+-BBS-")
        self.assertRegex(second.name, r"^02-a\d+-BBS-")

    def test_the_archive_covers_every_file_one_arrangement_produces(self):
        # Only one of six arrangements survives without this, so "which
        # arrangement was slow" stops being answerable.
        for f in ("verify.log", "k6.log", "metrics.json", "trace.csv"):
            self.assertIn(f, verify.ARRANGEMENT_FILES)

    def test_the_archive_keeps_the_logs_that_time_a_mount(self):
        # A mount's latency splits into decision and provisioning. When the
        # trace exposes no decision epoch — the common case — the service's
        # log is the only remaining source for the split.
        self.assertIn("service.deploy.log", verify.ARRANGEMENT_FILES)

    def test_the_gzipped_raw_k6_data_survives_into_the_archive(self):
        # The plain jsonl is gzipped away before archive_arrangement runs, so
        # the .gz is the only raw-k6 carrier left; without it in the list the
        # full per-request data survives for the LAST arrangement only.
        self.assertIn("k6_results.jsonl.gz", verify.ARRANGEMENT_FILES)
        # the blips' generators write their own raw data; the trace tailed
        # them, so the archive keeps them beside the main generator's
        for f in ("k6_blip_1.jsonl.gz", "k6_blip_2.jsonl.gz"):
            self.assertIn(f, verify.ARRANGEMENT_FILES)
        v = self.v(shape="BBS")
        v.prepare_out()
        (self.out / "k6_results.jsonl.gz").write_bytes(b"raw")
        (self.out / "k6_blip_1.jsonl.gz").write_bytes(b"blip")
        d = self.persist(v, verify.ARRANGEMENT_FILES)
        self.assertEqual((d / "k6_results.jsonl.gz").read_bytes(), b"raw")
        self.assertEqual((d / "k6_blip_1.jsonl.gz").read_bytes(), b"blip")

    def test_a_stale_gz_from_the_previous_arrangement_is_not_archived(self):
        # gzip runs late, so a run that fails before the load never overwrites
        # the previous arrangement's .gz — prepare_out must clear it or the
        # archive attributes the old load to the new arrangement.
        v = self.v(shape="BBS")
        (self.out).mkdir(parents=True, exist_ok=True)
        (self.out / "k6_results.jsonl.gz").write_bytes(b"previous")
        (self.out / "k6_blip_2.jsonl.gz").write_bytes(b"previous")
        v.prepare_out()
        self.assertFalse((self.out / "k6_results.jsonl.gz").exists())
        self.assertFalse((self.out / "k6_blip_2.jsonl.gz").exists())

    def test_the_archive_list_is_the_recorded_one(self):
        # The set every sealed cell's arrangements/ was archived with; a file
        # gained or lost here makes old and new cells answer different
        # questions about the same run. The blips' raw data joined the set
        # with the closed-loop blip (cells sealed before it carry none).
        self.assertEqual(set(verify.ARRANGEMENT_FILES), {
            "verify.log", "k6.log", "deploy.log", "teardown.log",
            "resource_sampler.log", "k6_summary.json", "k6_results.jsonl",
            "k6_results.jsonl.gz", "k6_blip_1.jsonl.gz", "k6_blip_2.jsonl.gz",
            "trace.csv", "trace_events.json",
            "schedule.json", "metrics.json", "service.deploy.log"})

    def test_the_archive_creates_its_directory_before_counting(self):
        # arrangements/ is created only BY the archive, so counting it before
        # creating it fails on the first arrangement of every cell.
        body = CELL_SRC[CELL_SRC.index("    def _archive(self"):]
        body = body[:body.index("\n    def ")]
        self.assertLess(body.index("base.mkdir("), body.index("base.iterdir()"))


class TestTheDefaultsAreTheRecordedNumbers(VerifyTestCase):
    """Most of these are inline defaults rather than config entries, so a
    change here changes the measurement without changing config.sh. The
    numbers are the ones every recorded cell ran under."""

    def test_the_inline_defaults_are_the_recorded_numbers(self):
        for var, default in (("VERIFY_READY_TIMEOUT_S", "90"),
                             ("VERIFY_READY_HOLD_S", "10"),
                             ("VERIFY_CELL_K6_DURATION_S", "100"),
                             ("CALIBRATION_MIN_QPS", "40"),
                             ("VU_PREALLOC", "8000"),
                             ("VU_MAX", "12000")):
            self.assertRegex(VERIFY_SRC, rf'"{var}"\)?\s*or {default}\b|"{var}", "{default}"',
                             f"{var} default drifted")

    def test_the_service_port_is_the_documented_8080_and_the_store_is_the_cells_own(self):
        # the docs name the service port (KEDA manifests target APP_HOST:8080);
        # the verify lock runs one service at a time, so it need not move
        v = self.v()
        # the service runs in the cell's secure runner, reached by its name
        self.assertEqual(v.base_url, f"http://fae-secrun-{v.cid}:8080")
        self.assertNotIn("SERVICE_PORT", v._env)
        self.assertEqual(v._env["DB_CONTAINER"], f"fae-store-{v.cid}")
        self.assertEqual(v._env["DB_NET"], f"fae-net-{v.cid}")
        # reached by name on the cell network: no host port is allocated
        self.assertEqual(v._env["DB_PORT"], "5432")

    def test_the_load_shape_defaults_are_the_declared_ones(self):
        # the [load] keys, as the block's instruments receive them
        v = self.v()
        self.assertEqual(v.load_params(), {"baseline": 18, "top": 360, "ramp_s": 20,
                                           "hold_s": "", "blip_p99_ms": 50.0,
                                           "blip_excess_req": 10,
                                           "sat_p99_ms": 150.0, "max_mount_s": 30.0})
        env = v.load_env()
        self.assertEqual(env["DISC_SPIKE"], "360")
        self.assertEqual(env["DISC_T_RAMP"], "20")
        # a blip is the spike's own ramp, stopped a known number of requests
        # past the first pressure
        self.assertEqual((env["BLIP_TOP"], env["BLIP_RAMP_S"], env["BLIP_P99_MS"], env["BLIP_EXCESS"]),
                         ("360", "20", "50.0", "10"))
        self.assertNotIn("DISC_BLIP", env, "the main generator drives no blip level")
        self.assertEqual(env["VERIFY_SAT_P99_MS"], "150.0")
        self.assertEqual(env["VERIFY_MAX_MOUNT_S"], "30.0")
        self.assertNotIn("DISC_T_SPIKE", env, "an empty hold_s leaves the block's default")

    def test_a_declared_hold_reaches_the_schedule(self):
        v = self.v(cfg={"LOAD_HOLD_S": "40"})
        self.assertEqual(v.load_env()["DISC_T_SPIKE"], "40")

    def test_a_declared_blip_rule_reaches_the_blip_generator(self):
        v = self.v(cfg={"LOAD_BLIP_P99_MS": "80", "LOAD_BLIP_EXCESS_REQ": "0"})
        env = v.load_env()
        self.assertEqual((env["BLIP_P99_MS"], env["BLIP_EXCESS"]), ("80.0", "0"))


class TestTheBlipsAreTheirOwnGenerator(VerifyTestCase):
    """A blip is driven by loadgen_blip.js, one process per blip window,
    started at the window's t0 counted from the main generator's first
    request and ended by its own stop rule; the main generator keeps the
    baseline underneath."""

    SCHED = {"shape": "BBS", "events": [{"kind": "blip", "t0": 8.0, "t1": 30.0},
                                        {"kind": "blip", "t0": 38.0, "t1": 60.0},
                                        {"kind": "spike", "t0": 68.0, "t1": 121.0}]}

    def main_output(self, first_request=None):
        """The main generator's k6 output, with its first request if any."""
        path = self.out / "k6_results.jsonl"
        lines = ['{"type":"Metric","metric":"fired","data":{"type":"counter"}}']
        if first_request is not None:
            stamp = datetime.fromtimestamp(first_request, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            lines.append(json.dumps({"type": "Point", "metric": "fired",
                                     "data": {"time": stamp, "value": 1}}))
        path.write_text("\n".join(lines) + "\n")
        return path

    def drive(self, v, sched, rc=108, wait_raises=None):
        """Run blips() to completion with a fake k6; the first request is
        long past, so is every window's t0, and nothing sleeps."""
        procs = []

        def popen(argv, **kw):
            p = mock.Mock(argv=argv, kw=kw)
            if wait_raises:
                p.wait.side_effect = wait_raises
            else:
                p.wait.return_value = rc
            procs.append(p)
            return p

        log = []
        with mock.patch.object(verify.subprocess, "Popen", side_effect=popen):
            outs, thread = v.blips(sched, self.main_output(verify.time.time() - 1000),
                                   lambda: True, log.append)
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        return outs, procs, log

    def test_one_blip_generator_per_blip_window_at_its_t0(self):
        v = self.v()
        outs, procs, log = self.drive(v, self.SCHED)
        self.assertEqual(outs, [v.out / "k6_blip_1.jsonl", v.out / "k6_blip_2.jsonl"])
        self.assertEqual(len(procs), 2, "two blip windows, two generators; the spike is not one")
        for i, p in enumerate(procs, 1):
            self.assertEqual(p.argv[:3], ["k6", "run", "--quiet"])
            self.assertIn(f"json={v.out / f'k6_blip_{i}.jsonl'}", p.argv)
            self.assertTrue(p.argv[-1].endswith("loadgen_blip.js"), p.argv)
        self.assertEqual([l[:6] for l in log], ["blip 1", "blip 2"])
        self.assertTrue(all("stopped by its rule" in l for l in log), log)

    def test_a_blip_starts_at_its_t0_counted_from_the_first_request(self):
        # the generator's start-up time is not part of the schedule
        v = self.v()
        first = verify.time.time() + 100
        waits = []
        with mock.patch.object(verify.subprocess, "Popen", return_value=mock.Mock(**{"wait.return_value": 108})), \
                mock.patch.object(verify.time, "sleep", side_effect=waits.append):
            _, thread = v.blips(self.SCHED, self.main_output(first), lambda: True, [].append)
            thread.start()
            thread.join(10)
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 100 + 8.0, delta=1.0)
        self.assertAlmostEqual(waits[1], 100 + 38.0, delta=1.0)

    def test_a_main_generator_that_ends_without_a_request_gets_no_blips(self):
        v = self.v()
        log = []
        with mock.patch.object(verify.subprocess, "Popen") as popen:
            _, thread = v.blips(self.SCHED, self.main_output(None), lambda: False, log.append)
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        popen.assert_not_called()
        self.assertEqual(len(log), 1)
        self.assertIn("sent no request", log[0])

    def test_the_blip_generator_reads_the_load_window_it_runs_in(self):
        # run_load sets the load env on os.environ for the window; a blip
        # inherits it, with a pool of its own size (it ends at the first
        # slow request, so it never needs the spike's)
        v = self.v()
        with mock.patch.dict(os.environ, {"BLIP_P99_MS": "50.0", "BASE_URL": "http://svc:8080",
                                          "VU_PREALLOC": "8000", "VU_MAX": "12000"}):
            _, procs, _ = self.drive(v, self.SCHED)
        env = procs[0].kw["env"]
        self.assertEqual((env["BLIP_P99_MS"], env["BASE_URL"]), ("50.0", "http://svc:8080"))
        self.assertEqual((env["VU_PREALLOC"], env["VU_MAX"]), ("200", "1000"))

    def test_a_blip_that_ran_its_whole_ramp_is_said_so(self):
        # the stop rule never fired: the ramp did not reach this host's knee
        _, _, log = self.drive(self.v(), self.SCHED, rc=0)
        self.assertTrue(all("ran its whole ramp" in l for l in log), log)

    def test_a_blip_that_outlives_its_ramp_is_killed(self):
        v = self.v()
        _, procs, log = self.drive(v, self.SCHED,
                                   wait_raises=subprocess.TimeoutExpired("k6", 35))
        for p in procs:
            p.kill.assert_called_once()
            self.assertEqual(p.wait.call_args.kwargs["timeout"], 20 + 15, "ramp_s + margin")
        self.assertTrue(all("rc=killed" in l for l in log), log)

    def test_no_schedule_means_no_blip_generator(self):
        v = self.v()
        outs, procs, log = self.drive(v, None)
        self.assertEqual((outs, procs, log), ([], [], []))

    def test_the_trace_tails_every_generators_output(self):
        # the law reads one trace: the blips' requests must land in it
        body = VERIFY_SRC[VERIFY_SRC.index("    def run_load(self"):]
        body = body[:body.index("\n    def ")]
        self.assertIn('for p in blip_outs for a in ("--k6-jsonl", p)', body)
        self.assertLess(body.index("blips.start()"), body.index('self.block("trace"'),
                        "the blips run under the trace")
        self.assertLess(body.index("k6.wait()"), body.index("blips.join("))

    def test_the_mount_delay_reads_the_schedule(self):
        # without it an external autoscaler's delay runs from a blip's queue
        body = VERIFY_SRC[VERIFY_SRC.index("    def run_load(self"):]
        body = body[:body.index("\n    def ")]
        call = body[body.index('self.block("k6"'):]
        call = call[:call.index(")") + 1]
        self.assertIn('"schedule.json"', call)

    def test_a_verify_with_no_shape_drives_the_canonical_arrangement(self):
        # so the law always judges in schedule mode, blips included
        v = self.v()
        with mock.patch.object(v, "block", return_value=(0, "")) as blk, \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VERIFY_SHAPE_SEED", None)
            v.load_shape()
        self.assertEqual(blk.call_args.kwargs["env"]["VERIFY_SHAPE"], verify.Verify.CANONICAL_SHAPE)
        self.assertEqual(verify.Verify.CANONICAL_SHAPE, "BBS")
        v = self.v(shape="SBB")
        with mock.patch.object(v, "block", return_value=(0, "")) as blk:
            v.load_shape()
        self.assertEqual(blk.call_args.kwargs["env"]["VERIFY_SHAPE"], "SBB")
        v = self.v()
        with mock.patch.object(v, "block", return_value=(0, "")) as blk, \
                mock.patch.dict(os.environ, {"VERIFY_SHAPE_SEED": "7"}):
            v.load_shape()
        self.assertNotIn("VERIFY_SHAPE", blk.call_args.kwargs["env"], "a seed picks the permutation")


class TestConfigIsReadWholeNotJustTheExportedPart(VerifyTestCase):

    def test_the_rig_defaults_are_read_whole(self):
        # The rig knobs are built-in defaults, not exported env; reading the
        # environment alone would return a config full of holes, and a hole
        # reads as a default, which is how a knob silently stops being one.
        from fae.experiment import config as cfgmod
        cfgmod._cache.clear()
        v = cfgmod.load(ROOT).values
        self.assertEqual(v["DB_CPUS"], "1")
        self.assertEqual(v["DB_MEM"], "512m")

    def test_the_real_config_yields_the_store_caps(self):
        v = verify.Verify(self.art, self.out, root=ROOT)
        self.assertTrue(v.cfg.get("DB_CPUS"), "DB_CPUS did not survive the read")
        self.assertTrue(v.cfg.get("DB_IMAGE"))


class TestTheRigLockIsHeldByTheCellProcess(VerifyTestCase):
    """The engine holds a verifier's declared exclusive lock on the cell's own
    fd around the verifier subprocess, which inherits no fd. This
    verifier declares none: its store is per cell."""

    def cell(self):
        c = Cell("cell-x", workspaces=self.d, root=ROOT)
        c.conf.values["RIG_LOCK_DIR"] = str(self.d / "rig-lock")
        c.conf.values["RIG_LOCK_WAIT_S"] = "1"
        return c

    def test_the_verifier_declares_no_exclusive_lock(self):
        self.assertIsNone(fae.experiment.exp().definition.exclusive)

    def test_acquiring_and_releasing_leaves_it_free(self):
        a, b = self.cell(), self.cell()
        fa = a.exclusive_acquire("rig", poll=0.1)
        self.assertIsNotNone(fa)
        self.assertIsNone(b.exclusive_acquire("rig", poll=0.1))      # singleton, as designed
        fa.close()
        fb = b.exclusive_acquire("rig", poll=0.1)
        self.assertIsNotNone(fb)
        fb.close()

    def test_the_lock_is_a_file_not_a_directory_of_state(self):
        fh = self.cell().exclusive_acquire("rig", poll=0.1)
        self.addCleanup(fh.close)
        self.assertTrue((self.d / "rig-lock").is_file())

    def test_children_do_not_inherit_it(self):
        # THE 2026-08-17 wedge: a backgrounded daemon inherited the rig lock and
        # held it after its run died. subprocess closes fds by default, so this
        # is structural here rather than a wrapper somebody must remember.
        fh = self.cell().exclusive_acquire("rig", poll=0.1)
        self.addCleanup(fh.close)
        p = verify.subprocess.run(
            ["bash", "-c", f'if [ -e /dev/fd/{fh.fileno()} ]; then echo INHERITED; '
                           f'else echo closed; fi'],
            capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "closed")


class TestPulumiStateStartsEmpty(VerifyTestCase):
    """The clean start wipes the cell's docker host before every pulumi
    verify; the Pulumi state that described those containers is wiped with
    it, or the program's next up/destroy targets containers that are gone."""

    def _ok_run(self, *a, **kw):
        m = mock.Mock()
        m.returncode = 0
        return m

    def test_a_pulumi_deploy_starts_from_an_empty_state(self):
        v = self.v(variant="pulumi_access_apidocs")
        state = v.out / "secrun-scratch" / ".pulumi-state"
        state.mkdir(parents=True)
        (state / "stale.json").write_text("{}")
        with mock.patch.object(v, "infra", return_value=mock.Mock(
                TAG="pulumi", verify_setup=mock.Mock(side_effect=verify.HookFailure("stop")))), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"):
            v.deploy("pulumi")
        self.assertFalse(state.exists())

    def test_the_state_backend_is_judged_before_the_service_runs(self):
        src = VERIFY_SRC if "VERIFY_SRC" in globals() else (ENGINE_TREE / "fae" / "cell" / "verify.py").read_text()
        body = src[src.index("    def deploy(self, tech):"):src.index("    def readiness(self):")]
        self.assertLess(body.index('shutil.rmtree(self.out / service.SCRATCH / ".pulumi-state"'),
                        body.index("before_deploy()"))
        self.assertLess(body.index("before_deploy()"), body.index("verify_setup("))

    def test_the_pulumi_teardown_waits_for_the_service_before_the_wipe(self):
        src = (Path(ROOT) / "experiment" / "verifier" / "__init__.py").read_text()
        body = src[src.index("    def teardown(self, tech):"):]
        self.assertLess(body.index(".stop_service()"), body.index("infra.verify_teardown("))
        src = (Path(ROOT) / "experiment" / "variants" / "pulumi" / "__init__.py").read_text()
        body = src[src.index("    def on_verify_teardown("):]
        self.assertNotIn("stop_service", body)
        self.assertIn("substrate.reset()", body)

    def test_other_techs_leave_the_pulumi_state_alone(self):
        v = self.v()
        state = v.out / "secrun-scratch" / ".pulumi-state"
        state.mkdir(parents=True)
        (state / "keep.json").write_text("{}")
        with mock.patch.object(verify.subprocess, "run", self._ok_run):
            v.deploy("keda")
        self.assertTrue((state / "keep.json").exists())


class TestTheKedaVerifyClusterIsItsOwn(VerifyTestCase):
    """keda_access's kube.env names the agent's sandbox cluster. The verify
    grades a throwaway cluster of its own, so it never adopts that
    kubeconfig, and after deploy the guards ask the verify cluster."""

    def test_kube_env_lends_only_the_namespace(self):
        v = self.v(variant="kedap_access_apidocs")
        (v.ws / "kube.env").write_text(
            "KUBECONFIG=/sandbox/kubeconfig.host\nKIND_CLUSTER=ecb-ka-1\nNAMESPACE=ecb\n")
        adopted = v.adopt_cell_env()
        self.assertEqual(adopted, {"NAMESPACE": "ecb"})
        self.assertNotIn("KUBECONFIG", v._env)

    def test_a_successful_deploy_adopts_the_verify_clusters_kubeconfig(self):
        v = self.v(variant="kedap_access_apidocs")
        v._env["KUBECONFIG"] = "/whatever/the/shell/had"
        cfg = v.out / ".kubecfg"
        with mock.patch.object(v, "infra", return_value=mock.Mock(
                TAG="conv", verify_setup=mock.Mock(
                    return_value=verify.service.Up(exported={"KUBECONFIG": str(cfg)})))), \
                mock.patch("fae.cell.infra.secrunner.stop_by_name"), \
                mock.patch.object(verify.service.Bringup, "start_service"), \
                mock.patch.object(verify.service.Bringup, "service_ready"):
            self.assertEqual(v.deploy("keda"), "ok")
        self.assertEqual(v._env["KUBECONFIG"], str(cfg))


class TestTheContractVoidsAndStandsDown(VerifyTestCase):
    """A world that does not match the docs after the bring-up is a rig
    fault: the arrangement is void, never a build verdict. After the
    teardown, the verdict stands and the debris is reported."""

    def _ok_run(self, *a, **kw):
        m = mock.Mock()
        m.returncode = 0
        return m

    def test_a_broken_promise_after_deploy_is_stage_contract(self):
        v = self.v(variant="kedap_sealed_apidocs")
        viol = [verify.contract.Violation("doc:x", "world y", "z")]
        fake = mock.Mock(after_deploy=lambda: viol, after_teardown=lambda: [])
        said = []
        with mock.patch.object(verify.subprocess, "run", self._ok_run), \
             mock.patch.object(verify.contract, "for_variant", lambda vv: fake), \
             mock.patch.object(v, "say", said.append), \
             mock.patch.object(v, "adopt_cell_env", return_value={}), \
             mock.patch.object(v, "db_up", return_value=(True, "")), \
             mock.patch.object(v, "bench_ceiling", return_value=60), \
             mock.patch.object(verify.rig, "clean_start_wait", return_value=True), \
             mock.patch.object(v, "deploy", return_value="ok"), \
             mock.patch.object(v, "readiness", return_value=(10, 10)), \
             mock.patch.object(v, "teardown"):
            r = v.run()
        self.assertEqual(r.stage_failed, "contract")
        self.assertEqual(r.contract, ["doc:x — world y [z]"])
        self.assertFalse(r.green)
        self.assertTrue(any(l.startswith("FAIL[contract]: doc:x") for l in said), said)

    def test_a_broken_promise_after_teardown_keeps_the_verdict_and_reports(self):
        v = self.v(variant="kedap_sealed_apidocs")
        viol = [verify.contract.Violation("doc:t", "cluster survived")]
        fake = mock.Mock(after_deploy=lambda: [], after_teardown=lambda: viol)
        said = []
        with mock.patch.object(verify.subprocess, "run", self._ok_run), \
             mock.patch.object(verify.contract, "for_variant", lambda vv: fake), \
             mock.patch.object(v, "say", said.append), \
             mock.patch.object(verify.rig, "db_down"), \
             mock.patch.object(verify.rig, "assert_no_cache_backend", return_value=None):
            v.teardown("keda")
        self.assertEqual(v.result.contract, ["teardown: doc:t — cluster survived"])
        self.assertEqual(v.result.stage_failed, "")
        self.assertTrue(any(l.startswith("CONTRACT[teardown]: doc:t") for l in said), said)


class TestTheFootprintIsScopedToTheCell(VerifyTestCase):

    def test_the_sampler_is_told_which_containers_are_the_cells(self):
        from fae.cell.infra.base import cksum
        v = self.v(variant="kedap_access_apidocs")
        own = v.own_containers()
        self.assertIn(f"fae-store-{v.cid}", own)
        self.assertNotIn(f"fae-dind-{v.cid}", own)       # the agent's, never the verify's
        self.assertIn(f"ecb-ka-{cksum(v.cid + '-verify')}-control-plane", own)
        self.assertIn(f"fae-secrun-{v.cid}", own)
        p = self.v(variant="pulumi_sealed_apidocs")
        self.assertEqual(p.own_containers(), [f"fae-store-{p.cid}", f"fae-vdind-{p.cid}",
                                              f"fae-secrun-{p.cid}"])


if __name__ == "__main__":
    unittest.main()
