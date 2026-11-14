"""app/health.py — the deploy contract every variant gates on.

/health is a fixed skeleton file, so its meaning is the rig's to define, not
the agent's. The rig starts a verify only while it holds 200, so it has to mean
"this service can serve", not "this process is alive": 503 while the store does
not answer.
"""
import sys
import unittest
from pathlib import Path

# Deliberately NOT via tests/_ctx: this module must import under the CELL venv
# (the only interpreter with fastapi), and _ctx pulls the orchestrator's deps.
SKELETON = Path(__file__).resolve().parents[1] / "task" / "skeleton"

sys.path.insert(0, str(SKELETON))

# fastapi lives in the CELL venv, not the orchestrator's interpreter. Skip
# rather than fail there, and run it explicitly with:
#   workspaces.nosync/.venv-template/bin/python3 -m unittest discover -s tests \
#       -t tests -p "test_health_contract.py"
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.health import make_health_router
    _HAVE_FASTAPI = True
except ModuleNotFoundError:                       # pragma: no cover
    _HAVE_FASTAPI = False


def _client(pool):
    app = FastAPI()
    app.include_router(make_health_router())
    app.state.pool, app.state.task, app.state.metrics = pool, "T1", {}
    return TestClient(app)


class _OKPool:
    def get_stats(self):
        return {"pool_size": 2, "requests_waiting": 0}

    def connection(self):
        class Ctx:
            async def __aenter__(s):
                class Conn:
                    async def execute(s2, q):
                        class Cur:
                            async def fetchone(s3):
                                return (1,)
                        return Cur()
                return Conn()

            async def __aexit__(s, *a):
                return False
        return Ctx()


class _DeadPool(_OKPool):
    def connection(self):
        class Ctx:
            async def __aenter__(s):
                raise OSError("connection refused")

            async def __aexit__(s, *a):
                return False
        return Ctx()


@unittest.skipUnless(_HAVE_FASTAPI, "needs the cell venv (fastapi)")
class TestHealthReflectsTheStore(unittest.TestCase):
    def test_reachable_store_is_200(self):
        r = _client(_OKPool()).get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_unreachable_store_is_503(self):
        """The whole point: alive-but-useless must not read as healthy."""
        r = _client(_DeadPool()).get("/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "unavailable")

    def test_missing_pool_is_503(self):
        self.assertEqual(_client(None).get("/health").status_code, 503)

    def test_the_failure_reason_is_reported(self):
        """deploy.log is the only forensic trail when a verify will not start,
        so the response has to say WHY, not just refuse."""
        self.assertIn("connection refused", _client(_DeadPool()).get("/health").json()["store"])

    def test_it_round_trips_rather_than_trusting_the_pool_object(self):
        """A pool can exist while the backend refuses connections; presence of
        the object is not evidence of reachability."""
        src = (SKELETON / "app" / "health.py").read_text()
        self.assertIn("SELECT 1", src)
        self.assertIn("fetchone", src)


@unittest.skipUnless(_HAVE_FASTAPI, "needs the cell venv (fastapi)")
class TestTheLoadSignalsStillWork(unittest.TestCase):
    """/metrics feeds the scaling gate; the health change must not disturb it."""

    def test_metrics_is_unaffected_by_store_state(self):
        r = _client(_DeadPool()).get("/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertIn("pool_pressure", r.json())

    def test_health_still_carries_the_pool_telemetry(self):
        body = _client(_OKPool()).get("/health").json()
        for k in ("pool_pressure", "p99_ms", "rps", "inflight", "pool_size", "pool_waiting"):
            self.assertIn(k, body)


if __name__ == "__main__":
    unittest.main()
