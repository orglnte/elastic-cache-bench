"""The doc each variant hands its agent — the study's independent variable.

Which doc a cell reads is decided by its variant file's [authoring.inputs]:
the engine copies exactly what the file names, so a renamed file is a
refused seed, never a silent fallback. These pins guard the other way the
variable changes: the file pointing at another doc, or a doc truncated in
place."""
import unittest

from _ctx import runs

# variant -> (the doc in the agent's workspace, its source, a floor on its lines)
PINNED = {
    "kedap_sealed_apidocs":  ("docs/keda.md", "any.kedap.apidocs.api.md", 120),
    "kedap_access_apidocs":  ("docs/keda.md", "any.kedap.apidocs.api.md", 120),
    "kedam_sealed_apidocs":  ("docs/keda.md", "any.kedam.apidocs.api.md", 120),
    "kedam_access_apidocs":  ("docs/keda.md", "any.kedam.apidocs.api.md", 120),
    "pulumi_sealed_apidocs": ("docs/pulumi.md", "any.pulumi.apidocs.api.md", 100),
    "pulumi_access_apidocs": ("docs/pulumi.md", "any.pulumi.apidocs.api.md", 100),
}


class TestEachVariantReadsItsPinnedDoc(unittest.TestCase):
    def setUp(self):
        self.d = runs.experiment.exp().definition

    def test_every_variant_is_pinned_and_every_pin_is_a_variant(self):
        self.assertEqual(set(self.d.ids), set(PINNED))

    def test_each_variant_hands_the_pinned_doc(self):
        for vid, (rel, name, _) in PINNED.items():
            with self.subTest(variant=vid):
                self.assertEqual(self.d.variant(vid).INPUTS[rel].name, name)

    def test_pinned_docs_exist_and_meet_their_floor(self):
        for vid, (rel, _, floor) in PINNED.items():
            with self.subTest(variant=vid):
                src = self.d.variant(vid).INPUTS[rel]
                self.assertGreaterEqual(len(src.read_text().splitlines()), floor)

    def test_every_variant_reads_the_one_prompt(self):
        for vid in PINNED:
            with self.subTest(variant=vid):
                self.assertEqual(self.d.variant(vid).INPUTS["TODO.md"].name, "T1.PROMPT.md")

    def test_the_docs_factor_names_the_level_the_doc_is(self):
        for vid in PINNED:
            with self.subTest(variant=vid):
                self.assertEqual(self.d.variant(vid).FACTORS["docs"], vid.rsplit("_", 1)[1])


if __name__ == "__main__":
    unittest.main()
