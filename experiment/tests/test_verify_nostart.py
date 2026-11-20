"""VERIFY_READY / VERIFY_NOSTART — the guard that stops rig faults being scored
as authoring failures.

Origin, 2026-08-02. Three separate infra faults in one day were recorded as
`ITER fail stage=deploy e2e=0/0` and counted against the agent:

  1. a provisioning daemon that could not bind its port (two cells, four attempts),
  2. a cell venv missing the skeleton's requirements, so the daemon booted while
     the SERVICE died importing psycopg_pool (two cells, five attempts),
  3. and seven cells from 2026-07-24..30 with the same signature, predating any
     of it.

The existing defence was a minimum-verify-duration check, which only ever caught
FAST faults — every case above took 400s+ and sailed through. The readiness poll
was no defence either: it breaks on the FIRST 200, so a service that answers once
and dies passes it.

So the rule is now: the deploy contract (GET :$SERVICE_PORT/health == 200, which
every arm declares identically) must HOLD for a confirmation window. If it does
not, the verify never started, the attempt is not consumed, and the same attempt
number is retried. The verify is experiment/verifier/__init__.py; these pin its source.
"""
import unittest
from pathlib import Path

from _ctx import ENGINE_TREE, ROOT

VERIFY = (Path(ROOT) / "experiment" / "verifier" / "__init__.py").read_text()
LEDGER = (Path(ENGINE_TREE) / "fae" / "cell" / "ledger.py").read_text()
SERVICE = (Path(ROOT) / "experiment" / "variants" / "service.py").read_text()
VARIANTS = {tech: (Path(ROOT) / "experiment" / "variants" / tech / "__init__.py").read_text()
            for tech in ("keda", "pulumi")}
INFRA_UP = "# the infra is up; from here every failure is the agent's"


def block(start, end):
    b = VERIFY[VERIFY.index(start):]
    return b[:b.index(end)]


def setup_body(tech):
    body = VARIANTS[tech][VARIANTS[tech].index("    def on_verify_setup("):]
    return body[:body.index("\n    def ", 10)]


READINESS = block("    def readiness(self):", "    def e2e(self):")
DEPLOY = block("    def deploy(self, tech):", "    def readiness(self):")
RUN = VERIFY[VERIFY.index("    def run(self):"):]


class TestTheContractIsTreatmentAgnostic(unittest.TestCase):
    """One probe for every arm. The health contract is declared once, in the
    shared bring-up module every variant starts its service through, and the
    sustained probe belongs in the shared verify — not duplicated per
    treatment, where it would drift."""

    def test_every_bringup_declares_the_same_health_contract(self):
        self.assertIn("/health", SERVICE)
        self.assertIn("SERVICE_PORT", SERVICE)
        # held once, by the verifier, after whichever infra brought the world up
        self.assertIn("b.service_ready()", DEPLOY)
        for tech in ("keda", "pulumi"):
            self.assertNotIn("service_ready()", setup_body(tech), f"{tech} holds the contract itself")

    def test_the_probe_lives_in_the_shared_verify_not_a_treatment(self):
        self.assertIn("VERIFY_READY_HOLD_S", READINESS)
        for tech, body in VARIANTS.items():
            self.assertNotIn("VERIFY_READY_HOLD_S", body,
                             f"{tech} duplicates the sustained probe; it must be shared")


class TestSustainedHealthIsRequired(unittest.TestCase):
    def test_the_hold_loop_breaks_on_the_first_failure(self):
        """A single 200 is not enough — that is what let a service answer once,
        die, and still be graded as an authoring failure."""
        self.assertRegex(READINESS,
                         r'for _ in range\(hold\):\s*\n\s*if self\.http\("GET", "/health"\)\[0\] != 200:\s*\n\s*break')

    def test_a_short_hold_is_a_nostart_not_a_verdict(self):
        self.assertRegex(RUN, r'if held < want:[\s\S]{0,300}stage_failed = "nostart"')

    def test_never_reaching_200_is_a_nostart(self):
        # The first loop's `else` returns (0, 0): held < want, so nostart.
        self.assertRegex(READINESS, r'else:\s*\n\s*return 0, 0')

    def test_a_leaked_cache_backend_is_a_nostart_not_a_verdict(self):
        """The clean-start guard's own comment says the debris is another run's
        leak; scoring it would consume a real attempt."""
        self.assertRegex(
            RUN,
            r'a cache backend is deployed and could[\s\S]{0,300}stage_failed = "nostart"')


class TestDeployFailureIsSplitByWhatTheBringUpRaises(unittest.TestCase):
    """A bring-up fails for two unrelated reasons that were once collapsed
    into one refunded branch: the rig failing to bring the infra up, and
    the infra REJECTING the agent's artifacts (kubectl apply on a
    malformed manifest, the agent's app dying at boot). Refunding the second
    class let an agent loop forever on its own broken manifest — the refund
    also erased the rejection from its history, so it reproduced the
    identical error every cycle (haiku keda_sealed/apidocs r6, 2026-08-03, 4
    cycles on one bad ClusterRole before the operator killed it)."""

    def test_rejected_is_scored_everything_else_is_refunded(self):
        self.assertLess(DEPLOY.index("except service.Rejected"), DEPLOY.index('return "rejected"'))
        self.assertLess(DEPLOY.index('return "rejected"'), DEPLOY.index("except (HookFailure"))
        self.assertLess(DEPLOY.index("except (HookFailure"), DEPLOY.index('return "nostart"'))
        self.assertRegex(RUN, r'if state == "rejected":[\s\S]{0,300}stage_failed = "deploy"')
        self.assertRegex(RUN, r'if state == "nostart":[\s\S]{0,200}stage_failed = "nostart"')

    def test_every_bringup_marks_where_the_infra_is_up(self):
        """All three arms have the same two phases; an arm that raised
        Rejected before its infra was up would score a rig fault, and one
        that raised a rig fault after would refund a rejection."""
        for tech in ("keda", "pulumi"):
            body = setup_body(tech)
            self.assertIn(INFRA_UP, body, f"{tech} bring-up never marks its infra ready")
            up = body.index(INFRA_UP)
            before, after = body[:up], body[up:]
            self.assertNotIn("Rejected(", before, f"{tech}: a rejection before the infra is up")
            self.assertNotIn("HookFailure(", after, f"{tech}: a rig fault after the infra is up")
        # the service is the agent's phase: started after the infra is up
        self.assertLess(DEPLOY.index("infra.verify_setup("), DEPLOY.index(INFRA_UP))
        self.assertLess(DEPLOY.index(INFRA_UP), DEPLOY.index("b.start_service("))
        self.assertLess(DEPLOY.index("b.start_service("), DEPLOY.index("b.service_ready()"))


class TestLedgerTreatsItAsANonVerdict(unittest.TestCase):
    def test_ledger_documents_both_events(self):
        self.assertIn("VERIFY_READY", LEDGER)
        self.assertIn("VERIFY_NOSTART", LEDGER)


if __name__ == "__main__":
    unittest.main()
