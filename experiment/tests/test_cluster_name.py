"""KindCluster.name_for: the in-process POSIX cksum must equal the cksum binary.

The keda variant derives kind cluster names as ecb-ka-<cksum(cid)> for the
sandbox and the per-verify cluster alike, and the reaper's owner protection
(_cluster_map) asks the same class. One formula, pinned against the real
cksum binary here.
"""
import subprocess
import unittest

from _ctx import ROOT, runs
from fae.cell.infra.base import cksum
from experiment.variants.keda import KindCluster


def _binary_cksum(data: bytes) -> int:
    out = subprocess.run(["cksum"], input=data, capture_output=True, check=True)
    return int(out.stdout.split()[0])


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
        name = KindCluster.name_for("haiku_high_kedap_access_apidocs_T1_r2")
        self.assertRegex(name, r"^ecb-ka-\d+$")
        self.assertEqual(name, KindCluster.name_for("haiku_high_kedap_access_apidocs_T1_r2"))

    def test_the_verify_cluster_is_named_from_the_cids_verify_id(self):
        # the per-verify cluster and the access arm's sandbox never share a name
        cid = "sonnet_high_kedap_access_apidocs_T1_r3"
        self.assertNotEqual(KindCluster.name_for(cid), KindCluster.name_for(f"{cid}-verify"))

    def test_the_reaper_asks_the_subject(self):
        cid = "sonnet_high_kedap_access_apidocs_T1_r3"
        self.assertEqual(runs.zombies._infra_of("kedap_access_apidocs", cid),
                         [("cluster", KindCluster.name_for(cid)),
                          ("cluster", KindCluster.name_for(f"{cid}-verify"))])
        # the cell names all it provisions: its variant's cluster, the
        # engine's verify container and cell network, the verifier's store
        self.assertEqual(runs.experiment.exp().workspace.named_cell(cid).provisions(),
                         [("cluster", KindCluster.name_for(cid)),
                          ("cluster", KindCluster.name_for(f"{cid}-verify")),
                          ("container", f"fae-verify-{cid}"), ("network", f"fae-net-{cid}"),
                          ("container", f"fae-store-{cid}")])


if __name__ == "__main__":
    unittest.main()
