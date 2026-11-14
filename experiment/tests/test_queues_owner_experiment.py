"""Nothing in this experiment names what lives in the engine's .queues/:
fae/conduct/_queues.py is its only owner (the engine's tests/test_queues_owner.py)."""
import unittest

from _ctx import ROOT

from test_queues_owner import offenders


class TestTheExperimentLeavesTheQueuesToTheEngine(unittest.TestCase):

    def test_no_module_names_a_queues_path(self):
        self.assertEqual(offenders(ROOT / "experiment"), [])


if __name__ == "__main__":
    unittest.main()
