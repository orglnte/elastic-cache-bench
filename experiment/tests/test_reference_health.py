"""The references' app/health.py is the skeleton's, plus the cache_mounted
field their caches answer: the store round-trip and the 503 on an
unreachable store stay the skeleton's."""
import unittest
from pathlib import Path

from _ctx import ROOT

EXP = Path(ROOT) / "experiment"
SKELETON = EXP / "task" / "skeleton" / "app" / "health.py"
REFERENCES = {
    "pulumi": EXP / "variants" / "pulumi" / "seed" / "reference" / "overlay" / "app" / "health.py",
    "kedam": EXP / "variants" / "keda" / "seed" / "reference_kedam" / "overlay" / "app" / "health.py",
}
CACHE_MOUNTED = (
    '        cache = getattr(st, "cache", None)\n'
    "        # True only while a cache backend actually exists and answers.\n"
    "        cache_mounted = await cache.is_mounted() if cache is not None else False\n"
    "\n",
    '            "cache_mounted": cache_mounted,\n',
)


def _health_route(src):
    return src[src.index('@router.get("/health")'):src.index('@router.get("/metrics")')]


class TestTheReferencesHealth(unittest.TestCase):
    def test_pulumis_is_the_skeletons_plus_cache_mounted(self):
        src = REFERENCES["pulumi"].read_text()
        for lines in CACHE_MOUNTED:
            self.assertEqual(src.count(lines), 1, lines)
            src = src.replace(lines, "")
        self.assertEqual(src, SKELETON.read_text())

    def test_every_references_health_route_is_the_same(self):
        routes = {name: _health_route(p.read_text()) for name, p in REFERENCES.items()}
        self.assertEqual(routes["pulumi"], routes["kedam"])
        self.assertIn("SELECT 1", routes["pulumi"])
        self.assertIn("status_code=200 if store_ok else 503", routes["pulumi"])


if __name__ == "__main__":
    unittest.main()
