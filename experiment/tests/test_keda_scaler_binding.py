"""keda-p and keda-m: one infra, two scaler bindings. The docs say which
scaler the ScaledObjects may use; the verify refuses a build that uses
another, charged to the build."""
import unittest

from _ctx import ROOT

from experiment import taint
from experiment.variants import keda
import fae.experiment

SEED = ROOT / "experiment" / "variants" / "keda" / "seed"


def _so(name, *types):
    return {"metadata": {"name": name},
            "spec": {"triggers": [{"type": t, "metadata": {}} for t in types]}}


class TestTheBindingCheck(unittest.TestCase):

    def test_every_trigger_of_the_bound_type_holds(self):
        objs = {"items": [_so("redis", "prometheus", "prometheus")]}
        self.assertIsNone(keda.scaler_violation(objs, "prometheus"))

    def test_one_foreign_trigger_is_named(self):
        objs = {"items": [_so("redis", "metrics-api", "prometheus")]}
        why = keda.scaler_violation(objs, "metrics-api")
        self.assertIn("redis:prometheus", why)
        self.assertIn("`metrics-api`", why)

    def test_a_foreign_trigger_on_a_second_scaledobject_is_named(self):
        objs = {"items": [_so("a", "prometheus"), _so("b", "cpu")]}
        self.assertIn("b:cpu", keda.scaler_violation(objs, "prometheus"))

    def test_no_scaledobject_is_a_violation(self):
        self.assertIn("no ScaledObject", keda.scaler_violation({"items": []}, "prometheus"))
        self.assertIn("no ScaledObject", keda.scaler_violation({}, "prometheus"))

    def test_scaledobjects_without_triggers_are_a_violation(self):
        self.assertIn("no ScaledObject has a trigger",
                      keda.scaler_violation({"items": [_so("redis")]}, "metrics-api"))


class TestTheVariants(unittest.TestCase):

    def test_the_bindings(self):
        d = fae.experiment.exp().definition
        self.assertEqual({v: d.variant(v).PARAMS.get("scaler") for v in d.ids if v.startswith("keda")},
                         {"kedap_sealed_apidocs": "prometheus", "kedap_access_apidocs": "prometheus",
                          "kedam_sealed_apidocs": "metrics-api", "kedam_access_apidocs": "metrics-api"})

    def test_one_infra_one_lock(self):
        from fae.cell.substrate import K8sSubstrate
        d = fae.experiment.exp().definition
        for v in (v for v in d.ids if v.startswith("keda")):
            cls = d.variant(v)
            self.assertTrue(issubclass(cls.INFRA, K8sSubstrate), v)
            self.assertIs(cls.SUBSTRATE.overlay, keda.KedaOverlay, v)
            self.assertEqual(cls.ACCESS_INFRA, "_access_" in v, v)
            self.assertEqual(cls.LOCK, "keda_access" if "_access_" in v else None, v)

    def test_the_check_runs_after_the_apply_and_before_the_service(self):
        src = (ROOT / "experiment" / "variants" / "keda" / "__init__.py").read_text()
        body = src[src.index("    def on_verify_setup("):src.index("    def not_ready(")]
        self.assertLess(body.index("substrate.apply("), body.index("scaler_violation("))
        self.assertLess(body.index("scaler_violation("), body.index("return service.Up("))
        self.assertIn("raise service.Rejected(why)", body)


class TestTheDocsStateTheBinding(unittest.TestCase):

    def test_keda_p_is_prometheus_only(self):
        doc = (SEED / "any.kedap.apidocs.api.md").read_text()
        self.assertIn("`prometheus` scaler, and only with it", doc)
        self.assertIn("recording rule", doc)
        self.assertNotIn("metrics-api", doc)

    def test_keda_m_is_metrics_api_only(self):
        doc = (SEED / "any.kedam.apidocs.api.md").read_text()
        self.assertIn("`metrics-api` scaler, and only with it", doc)
        self.assertIn("computed **in the app**", doc)
        self.assertNotIn("type: prometheus", doc)
        self.assertNotIn("recording rule", doc)

    def test_each_variant_is_seeded_its_own_doc(self):
        d = fae.experiment.exp().definition
        for vid, doc in (("kedap_sealed_apidocs", "any.kedap.apidocs.api.md"),
                         ("kedap_access_apidocs", "any.kedap.apidocs.api.md"),
                         ("kedam_sealed_apidocs", "any.kedam.apidocs.api.md"),
                         ("kedam_access_apidocs", "any.kedam.apidocs.api.md")):
            self.assertEqual(d.variant(vid).INPUTS["docs/keda.md"].name, doc)

    def test_the_access_variants_read_the_live_cluster_layout(self):
        d = fae.experiment.exp().definition
        for vid in ("kedap_access_apidocs", "kedam_access_apidocs"):
            self.assertEqual(d.variant(vid).INPUTS["docs/project.md"].name,
                             "T1.keda_access.project_layout.md")


class TestTheTaintRulesKnowTheTech(unittest.TestCase):

    def test_a_bound_variant_is_keda_to_the_junction_rules(self):
        self.assertEqual(taint._tech_of("kedap_access_apidocs"), "keda")
        self.assertEqual(taint._tech_of("kedam_sealed_apidocs"), "keda")


if __name__ == "__main__":
    unittest.main()
