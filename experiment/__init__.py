"""The elastic-cache-bench experiment — the definition the engine reads (fae/experiment/).

Its variants are the files in variants/*.toml: one per complete set of what
the agent is given. A variant id is <tech>_<access>_<docs>, and its file
records the three as factors:

- tech: keda (the KEDA families kedap, bound to the `prometheus`
  scaler, the policy in PromQL, and kedam, bound to `metrics-api`, the
  policy computed in the app) | pulumi;
- access: sealed = the agent authors blind, access = the agent gets the
  live environment;
- docs: apidocs = the agent gets the framework's API docs.
"""
from __future__ import annotations

from fae.experiment import Gate

NAME = "ecb"


# Six arrangements of the same three load events (B = short blip, S =
# sustained spike); GREEN means the policy handles every ordering, the
# multi-spike ones testing re-acquisition.
GATE = Gate(("BBS", "BSB", "SBB", "SSB", "SBS", "BSS"), feedback_note=(
    "NOTE: verification runs the load event sequence in SEVERAL arrangements\n"
    "(B=short blip, S=sustained spike; e.g. BBS, SBS). Your build passed: {passed}\n"
    "but FAILED on arrangement: {failed} (the verify.log below is from that run).\n"
    "ALL arrangements must pass — the policy must react to the load signals,\n"
    "whatever their order or count."))

# Machine-local configuration (fae.toml, or the environment).
CONFIG = {
    # The capped store the verifier benchmarks and every deploy serves from:
    # one per verify, fresh on a tmpfs, named and ported per cell by the
    # verifier (rig.cell_identity); only its image and caps are config.
    "DB_IMAGE": ("store", "image", "postgres:16.15", "str"),
    "DB_NAME": ("store", "name", "metadata", "str"),
    "DB_CPUS": ("store", "cpus", "1", "str"),
    "DB_MEM": ("store", "mem", "512m", "str"),
    "DB_MAX_CONN": ("store", "max_conn", "100", "str"),
    "DB_WARM_SECS": ("store", "warm_secs", "8", "str"),
    "DB_BENCH_CLIENTS": ("store", "bench_clients", "8", "str"),
    "DB_BENCH_SECS": ("store", "bench_secs", "10", "str"),
    # Below this store ceiling (qps) the host reads as overloaded: the verify
    # re-benches instead of judging.
    "CALIBRATION_MIN_QPS": ("rig", "calibration_min_qps", "40", "str"),
    # The load shape every arrangement drives (rps), and the two numbers the
    # law measures with: blips and the spike ramp from the baseline to the
    # top, so the ramp crosses the store's knee wherever this host puts it;
    # a blip rides the same ramp, and once a request goes over blip_p99_ms
    # or waits for the pool it keeps climbing until blip_excess_req requests
    # have been offered above that rate, then stops: a policy that mounts on
    # a fixed count of slow or queued requests must see that count without
    # a sustained overload behind it; the store counts
    # as saturated at sat_p99_ms and the cache must mount within
    # max_mount_s of that. A spike that never saturates the store
    # voids the arrangement (raise top). hold_s empty = the block's default
    # (30s, 25s in multi-spike arrangements).
    "LOAD_BASELINE_RPS": ("load", "baseline", "18", "str"),
    "LOAD_TOP_RPS": ("load", "top", "360", "str"),
    "LOAD_RAMP_S": ("load", "ramp_s", "20", "str"),
    "LOAD_HOLD_S": ("load", "hold_s", "", "str"),
    "LOAD_BLIP_P99_MS": ("load", "blip_p99_ms", "50", "str"),
    "LOAD_BLIP_EXCESS_REQ": ("load", "blip_excess_req", "10", "str"),
    "LOAD_SAT_P99_MS": ("load", "sat_p99_ms", "150", "str"),
    "LOAD_MAX_MOUNT_S": ("load", "max_mount_s", "30", "str"),
}


def verifier_class():
    from .verifier import ElasticCacheVerifier
    return ElasticCacheVerifier


def verify_host_probe():
    from .verifier.probe import StoreBenchProbe
    return StoreBenchProbe


def verbs():
    from . import exp1, taint
    return {"reference_cell": exp1._reference_cell, "selftest": taint.selftest}


def commands():
    from . import exp1
    return {"exp1": exp1.main}


def taint_rules(*a, **kw):
    from . import taint
    return taint.rules(*a, **kw)


def report_text(cell):
    from . import taint
    return taint.mount_report_text(cell)


def report_summary(cells, metrics_of, delta, metrics):
    from . import reporting
    return reporting.summary(cells, metrics_of, delta, metrics)


# Model id -> scoreboard row label. Ids mapped to the same label share a row.
# Grouping and display only: every cell record keeps the exact id it ran under.
POOLED_MODELS = {
    "claude-fable-5": "fable-5.1/5",
    "claude-fable-5-1": "fable-5.1/5",
    "Gemini 3.8 Flash (High)": "g-3.8flash",
    "Gemini 3.1 Pro (High)": "g31pro",
    "claude-sonnet-5": "sonnet-5.5/5",
    "claude-sonnet-5-5": "sonnet-5.5/5",
}
