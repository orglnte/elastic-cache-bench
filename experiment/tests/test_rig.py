"""Goldens for experiment/verifier/rig.py — the values the lib.sh → rig.py
conversion was proven byte-identical against (Phase 6 step 1).

Each golden was first pinned on the BASH function, then the py port was held
to it; when the bash rig retired (2026-08-29) the bash half of each pin went
with it and the golden values remain as the py contract. A change to any of
them is a change to what every cell was measured against.

The docker-bound functions (db_up/db_down/db_assert_caps/db_bench_qps,
assert_no_cache_backend/force_clean_cache_backend, clean_start_wait) are NOT
pinned here — a unit suite must not require the rig. Their behaviour is
proven by the ws-test validation cell that runs the path end to end.
"""
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401

from experiment.verifier import rig as xrig  # noqa: E402


class TestMemBytes(unittest.TestCase):
    def test_golden_values(self):
        for v, want in (("512m", 536870912), ("1g", 1073741824),
                        ("64k", 65536), ("12345", 12345)):
            self.assertEqual(xrig.mem_bytes(v), want, v)


if __name__ == "__main__":
    unittest.main()


class TestTheVerifierTearsDownItsStore(unittest.TestCase):
    """A verify that did not finish leaves its store beside the dead
    container; the verifier's teardown removes it by the cell's name."""

    def test_the_store_named_for_the_cell_is_removed(self):
        from unittest import mock
        from fae.cell.verify import Ctx
        from experiment.verifier import ElasticCacheVerifier
        ctx = Ctx(root="/r", experiment_dir="/r/experiment", workspace="/w", artifacts="/w/a",
                  out="/w/o", cid="m_high_pulumi_sealed_apidocs_T1_r1", task="T1",
                  variant="pulumi_sealed_apidocs")
        with mock.patch.object(xrig, "_run") as run:
            ElasticCacheVerifier().teardown(ctx)
        run.assert_called_once_with(["docker", "rm", "-f",
                                     f"{xrig.STORE_PREFIX}m_high_pulumi_sealed_apidocs_T1_r1"])
