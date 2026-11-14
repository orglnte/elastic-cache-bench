"""The variants package resolves the harness and repo roots from its own
depth, so the daemon config helper and the skeleton are found."""
from __future__ import annotations

import unittest

from _ctx import ENGINE_TREE, ROOT
from fae.experiment import variants as tr


class TestPackageRoots(unittest.TestCase):
    def test_harness_is_the_harness_dir(self):
        self.assertEqual(tr.HARNESS, ENGINE_TREE / "fae")
        self.assertTrue((tr.HARNESS / "cell" / "ledger.py").is_file())

    def test_root_is_the_repo(self):
        self.assertEqual(tr.ROOT, ROOT)
        self.assertTrue((tr.ROOT / "experiment" / "task" / "skeleton").is_dir())
        self.assertTrue((tr.ROOT / "experiment" / "variants" / "pulumi" / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
