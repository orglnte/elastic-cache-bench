"""Properties of the verifier's own source that the engine relies on.

They used to sit in the engine's suite, which read this file from the root;
the experiment is its own repo now, so the pins travel with the code they pin.
"""
import re
import unittest
from pathlib import Path

from _ctx import ROOT

SRC = (Path(ROOT) / "experiment" / "verifier" / "__init__.py").read_text()


def _method(name):
    body = SRC[SRC.index(f"    def {name}"):]
    return body[:body.index("\n    def ", 10)]


class TestTheVerifierReleasesAndRecords(unittest.TestCase):

    def test_the_verifier_tears_down_on_every_path(self):
        body = SRC[SRC.index("    def run(self):"):]
        self.assertIn("finally:", body)
        tail = body[body.index("finally:"):]
        self.assertIn("self.teardown(", tail, "teardown is not on the unconditional path")

    def test_a_host_overload_is_recorded_in_the_ledger(self):
        # verify.log is truncated per verify; the ledger is what survives
        loop = SRC[SRC.index("while r.ceiling_qps is None or r.ceiling_qps < floor:"):]
        loop = loop[:loop.index("lp = self.load_params()")]
        self.assertIn('self._ledger("ALERT"', loop)
        self.assertIn("HOST-OVERLOADED", loop)
        self.assertIn("record_event(self.out", _method("_ledger"))

    def test_the_verify_writes_everything_under_out_dir(self):
        # out_dir is what makes a re-verify non-destructive
        block = SRC[SRC.index("    def prepare_out(self):"):SRC.index("    def db_up(self):")]
        self.assertIn("self.out", block)
        self.assertNotIn("self.ws", block)


class TestTheLoadRunsInProcess(unittest.TestCase):

    def test_gzip_is_the_module_not_the_binary(self):
        self.assertNotIn('"gzip"', SRC)
        self.assertIn("import gzip", SRC)

    def test_run_load_owns_one_environment_window(self):
        body = _method("run_load")
        i_set = body.index("os.environ.update")
        i_spawn = body.index("run_in_thread(")
        i_join = body.index("sampler.join")
        i_restore = body.rindex("os.environ.update")
        self.assertLess(i_set, i_spawn, "the window opens before the sampler")
        self.assertLess(i_join, i_restore, "the window closes after the join")
        self.assertIn("finally:", body)
        for c in re.findall(r'(?:run_in_thread|call)\([^)]*\)', body):
            self.assertNotIn("env=env", c,
                             "an overlapping instrument gets a private env patch")


if __name__ == "__main__":
    unittest.main()
