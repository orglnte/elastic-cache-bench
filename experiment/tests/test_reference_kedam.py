"""The KEDA-hint (kedam) reference: the cache law computed in the app,
exported as the `cache_demand` field of /metrics, read by a `metrics-api`
trigger and nothing else."""
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from _ctx import ROOT

VARIANTS = Path(ROOT) / "experiment" / "variants"
OVERLAY = VARIANTS / "keda" / "seed" / "reference_kedam" / "overlay"
PULUMI_POLICY = VARIANTS / "pulumi" / "seed" / "reference" / "overlay" / "app" / "cache_policy.py"


def _code(path):
    """The policy's code, without its module docstring."""
    src = path.read_text()
    return src[src.index("from __future__"):]


def _run(body):
    """Run `body` with the overlay's app package importable, in a child, so
    no other test's `app` package is shadowed."""
    prog = f"import sys; sys.path.insert(0, {str(OVERLAY)!r})\n" + textwrap.dedent(body)
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr)
    return r.stdout.split()


class TestTheVariantsUseIt(unittest.TestCase):
    def test_both_kedam_variants_name_it_and_no_other_does(self):
        for f in VARIANTS.glob("*.toml"):
            names_it = "reference_kedam/overlay" in f.read_text()
            self.assertEqual(names_it, f.name.startswith("kedam_"), f.name)


class TestTheLawIsThePulumiReferences(unittest.TestCase):
    def test_the_policy_code_is_the_same(self):
        self.assertEqual(_code(OVERLAY / "app" / "cache_policy.py"), _code(PULUMI_POLICY))


class TestTheScaler(unittest.TestCase):
    SO = (OVERLAY / "k8s" / "scaledobject.yaml").read_text()

    def test_every_trigger_is_metrics_api(self):
        types = re.findall(r"^\s*- type:\s*(\S+)", self.SO, re.M)
        self.assertEqual(types, ["metrics-api"])

    def test_it_reads_the_exported_field_from_the_app(self):
        self.assertIn('url: "http://APP_HOST:8080/metrics"', self.SO)
        self.assertIn('valueLocation: "cache_demand"', self.SO)

    def test_no_scraper_or_rule_engine(self):
        self.assertEqual(sorted(p.name for p in (OVERLAY / "k8s").iterdir()),
                         ["redis.yaml", "scaledobject.yaml"])

    def test_metrics_exports_the_field(self):
        src = (OVERLAY / "app" / "health.py").read_text()
        route = src[src.index('@router.get("/metrics")'):]
        self.assertIn('"cache_demand": m.get("cache_demand", 0)', route)


class TestTheExportedField(unittest.TestCase):
    def test_zero_from_the_start_one_once_mounted_zero_after_release(self):
        out = _run("""
            from app.cache import CacheController
            m = {"pool_pressure": 0.0, "p99_ms": 0.0}
            c = CacheController(m, client_factory=None)
            seen = [m["cache_demand"]]
            m.update(pool_pressure=1.0, p99_ms=500.0)
            for _ in range(c._policy.cfg.mount_samples - 1):
                c.tick(120.0)
            seen.append(m["cache_demand"])
            c.tick(120.0)
            seen.append(m["cache_demand"])
            m.update(pool_pressure=0.0, p99_ms=5.0)
            for _ in range(c._policy.cfg.release_samples):
                c.tick(120.0)
            seen.append(m["cache_demand"])
            for _ in range(c._policy.cfg.release_samples):
                c.tick(10.0)
            seen.append(m["cache_demand"])
            print(*seen)
        """)
        self.assertEqual(out, ["0", "0", "1", "1", "0"])


class TestDiscovery(unittest.TestCase):
    def test_reads_use_the_cache_only_while_it_answers(self):
        out = _run("""
            import asyncio
            from app.cache import CacheController

            class Redis:
                up = True
                closed = 0
                async def get(self, key):
                    if not Redis.up:
                        raise ConnectionError("gone")
                    return None
                async def aclose(self):
                    Redis.closed += 1

            async def main():
                c = CacheController({}, client_factory=Redis)
                await c.discover(); a = c._client is not None
                Redis.up = False
                await c.discover(); b = c._client is not None
                Redis.up = True
                await c.discover(); d = c._client is not None
                print(a, b, d, Redis.closed)
            asyncio.run(main())
        """)
        self.assertEqual(out, ["True", "False", "True", "1"])


if __name__ == "__main__":
    unittest.main()
