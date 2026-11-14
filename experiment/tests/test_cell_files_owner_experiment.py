"""Nothing in this experiment names a cell's own files or writes the
transitions log: Cell is their only reader and writer (the engine's
tests/test_cell_files_owner.py)."""
import unittest

from _ctx import ROOT

from test_cell_files_owner import offenders


class TestTheExperimentReadsCellsThroughCell(unittest.TestCase):

    def test_no_module_names_a_cell_file(self):
        # the verifier writes the verify's outputs; the infra classes are the
        # Cell's parts and own their env files and run dirs; pulumi.env is
        # named for the scan explicitly
        self.assertEqual(offenders(ROOT / "experiment", producers=("verifier", "variants"),
                                   names=("pulumi.env",)), [])


if __name__ == "__main__":
    unittest.main()
