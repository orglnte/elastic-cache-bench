"""What `experiment calibrate` reads from this experiment's verifier: the
config keys a host is calibrated with, the negative control it must refund,
and the values each reference arrangement measured."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import ROOT  # noqa: F401  (sys.path)
from test_verify_port import VerifyTestCase

import fae.experiment
from fae.cell import Cell, Verdict
from fae.cell.contrib.elastic_resource import calibration as contrib
from fae.experiment.config import Config
from experiment import CONFIG
from experiment import verifier as verify

V = verify.ElasticCacheVerifier
T0 = 1_000_000.0
SCHED = {"events": [{"kind": "blip", "t0": 2.0, "t1": 6.0},
                    {"kind": "spike", "t0": 10.0, "t1": 30.0}]}


def arrangement(d, p99_at_sat=170.0, done_catches_up=True):
    """An archived arrangement: one line per second, p99 `p99_at_sat` at
    second 16, the cache up over [22, 35); k6_done equals k6_sent, or never
    reaches it after the spike when `done_catches_up` is False."""
    rows = []
    for s in range(51):
        sent = s * 10
        done = sent if done_catches_up or s < 30 else 290
        rows.append({"epoch": T0 + s, "offered_rps": 100 + s,
                     "p99_ms": p99_at_sat if s == 16 else 20.0,
                     "infra_cache_up": str(22 <= s < 35), "k6_sent": sent, "k6_done": done})
    with (d / "trace.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (d / "trace_events.json").write_text(json.dumps({"k6_start_epoch": T0}))
    (d / "schedule.json").write_text(json.dumps(SCHED))
    return d


class TestCalibrationKeys(unittest.TestCase):
    def test_they_are_every_load_key_of_the_config(self):
        load = {k for k, (section, *_) in CONFIG.items() if section == "load"}
        self.assertEqual(set(V.CALIBRATION_KEYS), load)
        self.assertIn("LOAD_TOP_RPS", V.CALIBRATION_KEYS)
        self.assertNotIn("DB_CPUS", V.CALIBRATION_KEYS)


class TestCalibrationGuard(VerifyTestCase):
    def test_it_lowers_the_top_on_an_active_variant(self):
        g = V.CALIBRATION_GUARD
        self.assertIn(g["variant"], fae.experiment.exp().definition.active)
        self.assertEqual(g["env"], {"LOAD_TOP_RPS": 60})

    def test_its_stage_is_the_one_a_load_shape_refund_archives(self):
        v = self.v()
        v.result.load_total, v.result.load_errors = "6000", "0"
        (v.out / "iterations.log").write_text("")
        with mock.patch.object(v, "block", return_value=(0, "void|spike 1 never saturated")):
            v.scaling_gate(v.out / "trace.csv", v.out / "trace_events.json")
        c = Cell("cell-x", workspaces=self.d, root=ROOT)
        c._env.update({"TASK": v.task, "VARIANT": v.variant_id})
        stage = v.result.stage_failed
        c._persist_verdict(v.out, Verdict(ok=False, stage=stage, charge=stage not in verify.VOID_STAGES,
                                          metrics=v.result.to_metrics(),
                                          arrangement=v.arrangement_label()))
        archived = sorted((v.out / "arrangements").iterdir())[-1]
        verdict = json.loads((archived / "verdict.json").read_text())
        self.assertEqual((verdict["end_state"], verdict["stage"]),
                         ("refunded", V.CALIBRATION_GUARD["stage"]))


class TestCalibrationValues(unittest.TestCase):
    def values(self, cfg, **kw):
        conf = Config(cfg, {})
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_MAX_LAG_S"}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fae.experiment, "exp",
                                  return_value=SimpleNamespace(config=lambda: conf)):
            return V.calibration_values(arrangement(Path(td), **kw))

    def test_they_are_the_contribs_values(self):
        with tempfile.TemporaryDirectory() as td:
            want = contrib.values_from(arrangement(Path(td)), sat_p99=150.0)
        self.assertEqual(self.values({"LOAD_SAT_P99_MS": "150"}), want)
        self.assertEqual(want["spike1_sat_s"], 6.0)

    def test_saturation_is_read_at_the_configs_p99(self):
        self.assertIn("spike1_sat_s", self.values({"LOAD_SAT_P99_MS": "150"}))
        self.assertNotIn("spike1_sat_s", self.values({"LOAD_SAT_P99_MS": "180"}))

    def test_an_unset_p99_is_the_configs_default(self):
        self.assertEqual(self.values({}, p99_at_sat=149.0).get("spike1_sat_s"), None)
        self.assertEqual(self.values({}, p99_at_sat=150.0)["spike1_sat_s"], 6.0)

    def test_the_lag_is_capped_where_the_law_caps_it(self):
        late = {"done_catches_up": False}
        self.assertEqual(self.values({}, **late)["spike1_lag_s"], 20.0)
        self.assertEqual(self.values({"VERIFY_MAX_LAG_S": "7"}, **late)["spike1_lag_s"], 7.0)


class TestEstimatedSpread(unittest.TestCase):
    def test_it_is_the_contribs(self):
        self.assertEqual(V.estimated_spread("spike1_mount_s", 6.0),
                         contrib.estimated_spread("spike1_mount_s", 6.0))


if __name__ == "__main__":
    unittest.main()
