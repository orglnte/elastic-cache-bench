"""The definition as the engine reads it: its variants, their techs, the gate."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT, runs, OrchTmpCase  # noqa: F401
import re
import sys

import fae.experiment
from fae import paths as _paths
from fae.experiment import config as _config
HARNESS = _paths.ENGINE
PKG = HARNESS / "cell"

class TestDefinition(unittest.TestCase):
    def test_the_experiment_declares_its_variants(self):
        d = fae.experiment._experiment.load(ROOT / "experiment")
        self.assertEqual(d.name, "ecb")
        self.assertEqual(set(d.active), {
            "kedap_sealed_apidocs", "kedap_access_apidocs", "kedam_sealed_apidocs",
            "kedam_access_apidocs", "pulumi_sealed_apidocs", "pulumi_access_apidocs"})
        self.assertEqual(set(d.ids), set(d.active))
        self.assertEqual(d.gate.arrangements_nr, 6)
        self.assertIn("exp1", d.commands)
        self.assertIs(sys.modules["experiment"], d.module)

    def test_loading_by_name_and_by_path_is_one_object(self):
        import experiment  # the tests import it by name; the engine by path
        self.assertIs(fae.experiment._experiment.load(ROOT / "experiment").module, experiment)


class TestTheTechIsTheInfraClass(unittest.TestCase):
    """A variant's tech is the module of the infra class its file names; the
    engine keeps no table of variant names, current or retired."""

    def test_each_variant_and_its_tech(self):
        from _ctx import tech_of
        d = fae.experiment.exp().definition
        self.assertEqual({v: tech_of(d.variant(v)) for v in d.ids}, {
            "kedap_sealed_apidocs": "keda", "kedap_access_apidocs": "keda",
            "kedam_sealed_apidocs": "keda", "kedam_access_apidocs": "keda",
            "pulumi_sealed_apidocs": "pulumi",
            "pulumi_access_apidocs": "pulumi"})


if __name__ == "__main__":
    unittest.main()
