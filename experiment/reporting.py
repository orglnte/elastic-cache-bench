"""What this experiment adds to the aggregate's summary: the sealed-vs-access
gap per family (the variants that share a label) and how the analyst reads
the table."""
from __future__ import annotations
import fae.experiment


def _label_of(variant):
    cls = fae.experiment.exp().definition.variant(variant)
    return cls.LABEL if cls is not None else variant


NOTES = (
    "first_pass_correct_rate=H1b (human static) and e2e_green_rate=H1b (auto live). mean_iterations_to_green=H1c "
    "(proxy; budget_censored_rate flags cells that never went green within "
    "ATTEMPT_BUDGET). load_error_rate_on_green is the parity check measured on "
    "e2e-green builds only (speed/throughput parity is conceded — do NOT lead with "
    "it). access_minus_sealed separates doc-access effect from structure. Report "
    "n per cell and coverage."
)


def access_minus_sealed_by_label(cells, metrics_of, delta, metrics):
    """Per family (a label), per metric: access − sealed over the cells of
    each access level. A family with cells on neither side is absent; one
    side alone yields None deltas rather than a number against nothing."""
    gap = {}
    for label in sorted({_label_of(c.get("variant")) for c in cells if c.get("variant")}):
        mine = [c for c in cells if _label_of(c.get("variant")) == label]
        sealed = [c for c in mine if (c.get("factors") or {}).get("access") == "sealed"]
        access = [c for c in mine if (c.get("factors") or {}).get("access") == "access"]
        if sealed or access:
            sealed_m = metrics_of(sealed) if sealed else {}
            access_m = metrics_of(access) if access else {}
            gap[label] = {m: delta(access_m.get(m), sealed_m.get(m)) for m in metrics}
    return gap


def summary(cells, metrics_of, delta, metrics):
    return {"access_minus_sealed_by_label": access_minus_sealed_by_label(cells, metrics_of, delta, metrics),
            "notes": NOTES}
