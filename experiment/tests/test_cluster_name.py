"""K8sSubstrate cluster naming: the in-process POSIX cksum must equal the
cksum binary, and the reaper asks the variant's substrate."""
import subprocess
import unittest

from _ctx import ROOT, runs  # noqa: F401
from fae.cell.infra.base import cksum
from fae.cell.substrate import cluster_name_for


def _binary_cksum(data: bytes) -> int:
    out = subprocess.run(["cksum"], input=data, capture_output=True, check=True)
    return int(out.stdout.split()[0])


def _name(cid):
    return cluster_name_for(cid, "ecb-ka-")


class TestCksumMatchesBinary(unittest.TestCase):
    VECTORS = [
        "",
        "a",
        "gemini_high_kedap_access_apidocs_T1_r1",
        "opus_high_pulumi_sealed_apidocs_T1_r10",
        "x" * 10000,
        "".join(chr(i) for i in range(256)),
    ]

    def test_vectors_match_cksum_binary(self):
        for v in self.VECTORS:
            self.assertEqual(cksum(v), _binary_cksum(v.encode()), v[:40])

    def test_cluster_name_shape(self):
        name = _name("haiku_high_kedap_access_apidocs_T1_r2")
        self.assertRegex(name, r"^ecb-ka-\d+$")
        self.assertEqual(name, _name("haiku_high_kedap_access_apidocs_T1_r2"))

    def test_the_verify_cluster_is_named_from_the_cids_verify_id(self):
        cid = "sonnet_high_kedap_access_apidocs_T1_r3"
        self.assertNotEqual(_name(cid), _name(f"{cid}-verify"))

    def test_the_reaper_asks_the_subject(self):
        cid = "sonnet_high_kedap_access_apidocs_T1_r3"
        self.assertEqual(runs.zombies._infra_of("kedap_access_apidocs", cid),
                         [("cluster", _name(cid)),
                          ("cluster", _name(f"{cid}-verify"))])
        self.assertEqual(runs.experiment.exp().workspace.named_cell(cid).provisions(),
                         [("cluster", _name(cid)),
                          ("cluster", _name(f"{cid}-verify")),
                          ("container", f"fae-verify-{cid}"), ("network", f"fae-net-{cid}"),
                          ("container", f"fae-store-{cid}")])


if __name__ == "__main__":
    unittest.main()
