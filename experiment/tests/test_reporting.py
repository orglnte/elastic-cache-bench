"""experiment/reporting.py — what this experiment adds to the aggregate's
summary: the access − sealed gap per label, computed with the engine's own
per-group metric function and delta."""
import unittest

from _ctx import ROOT  # noqa: F401

from experiment import reporting
from experiment import report_summary


def metrics_of(cells):
    return {"green_rate": sum(1 for c in cells if c["green"]) / len(cells),
            "n_cells": len(cells)}


def delta(a, b):
    return None if a is None or b is None else round(a - b, 4)


def _cell(variant, green):
    access = "access" if "_access_" in variant else "sealed"
    return {"variant": variant, "factors": {"access": access}, "green": green}


CELLS = [
    _cell("pulumi_sealed_apidocs", True), _cell("pulumi_sealed_apidocs", False),
    _cell("pulumi_access_apidocs", True),
    _cell("kedap_sealed_apidocs", True), _cell("kedap_access_apidocs", False),
    _cell("kedam_access_apidocs", True),
]


class TestTheGap(unittest.TestCase):
    def test_access_minus_sealed_per_label(self):
        gap = reporting.access_minus_sealed_by_label(CELLS, metrics_of, delta, ["green_rate"])
        self.assertEqual(gap["Pulumi"], {"green_rate": 0.5})        # 1.0 − 0.5
        self.assertEqual(gap["KEDA"], {"green_rate": -1.0})         # 0.0 − 1.0
        self.assertEqual(gap["KEDA-hint"], {"green_rate": None})    # access only
        self.assertEqual(set(gap), {"Pulumi", "KEDA", "KEDA-hint"})

    def test_a_label_with_no_cells_is_absent(self):
        gap = reporting.access_minus_sealed_by_label(CELLS[:3], metrics_of, delta, ["green_rate"])
        self.assertEqual(set(gap), {"Pulumi"})

    def test_keda_is_the_prometheus_family(self):
        self.assertEqual(reporting._label_of("kedap_sealed_apidocs"), "KEDA")
        self.assertEqual(reporting._label_of("kedam_sealed_apidocs"), "KEDA-hint")

    def test_the_definition_hands_the_summary_block_its_entries(self):
        out = report_summary(CELLS, metrics_of, delta, ["green_rate"])
        self.assertEqual(set(out), {"access_minus_sealed_by_label", "notes"})
        self.assertIn("mean_iterations_to_green", out["notes"])


if __name__ == "__main__":
    unittest.main()
