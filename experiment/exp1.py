"""exp1: the reference-mechanism benchmark. One canonical arrangement per variant,
scored on the SAME rig a scored cell uses (deploy, e2e, load, scaling gate),
but on the reference implementation — no agent, no authoring, so its numbers
are the floor every variant's numbers are read against.

Unrelated to conduct — it queues behind live verifies through the same
verify-lock, never overlaps one, but the scheduler itself does not call it.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import time
from datetime import datetime
from pathlib import Path

import ujson as json
import fae.experiment



EXP1_VARIANTS = ["kedap_sealed_apidocs", "kedam_sealed_apidocs",
                 "pulumi_sealed_apidocs"]  # matched sealed variants


def log_epoch(line):
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").timestamp() if m else None


def pulumi_log_marks(log_path):
    """decision, first engine output, stack created, app answering."""
    dec = up0 = created = answer = None
    try:
        lines = log_path.read_text().splitlines()
    except FileNotFoundError:
        return dec, up0, created, answer
    for line in lines:
        if dec is None and "sustained saturation -> mounting cache" in line:
            dec = log_epoch(line)
        elif up0 is None and "pulumi up: Updating (cache)" in line:
            up0 = log_epoch(line)
        elif created is None and re.search(r"pulumi up:.*pulumi:pulumi:Stack \S+ created", line):
            created = log_epoch(line)
        elif answer is None and "cache mounted and answering" in line:
            answer = log_epoch(line)
    return dec, up0, created, answer


def span(a, b):
    return round(b - a, 2) if (a is not None and b is not None) else None


def first_spike_mount_s(cell):
    """Saturation -> mount for the run's first spike, measured as the k6
    block measures it (the spike's own saturation, the mount that starts in
    it); None when the run has no schedule or either end is missing."""
    sched = cell.evidence("schedule.json")
    if not sched.is_file():
        return None
    from fae.cell.contrib import elastic_resource
    spec = importlib.util.spec_from_file_location("_elastic_resource_k6",
                                                  elastic_resource.DIR / "k6.py")
    k6 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(k6)
    spikes = [w for w in json.loads(sched.read_text())["events"] if w["kind"] == "spike"]
    ev = json.loads(cell.evidence("trace_events.json").read_text())
    delays = k6.spike_mount_delays(k6.read_mounts(str(cell.evidence("trace.csv")), ev), spikes,
                                   float(ev.get("sat_p99_ms") or 150))
    return delays[0] if delays else None


def _reference_cell(cid, variant, rep, workspaces):
    """A Cell over a REFERENCE workspace: the variant's seed with its known
    answer laid over, no agent. Constructed only; prepare() seeds it."""
    return fae.experiment.exp().cell(cid, "T1", variant, rep, reference=True,
                                      workspaces=workspaces)


def _exp1_verify(cid, variant, rep):
    """One reference verify, in-process: prepare fresh (the known answer),
    the variant's infra (its setup), ONE canonical-shape Verify
    under the fleet's verify lock, teardown. Returns the VerifyResult.

    The same objects a scored cell runs — Cell.prepare / setup / verify /
    teardown — so a reference run is bit-identical to a cell verify; only the
    loop around them (attempts, the ledger's verdict) is absent. The lock is
    the fleet's, so exp1 queues behind live verifies and never overlaps one.
    """
    c = _reference_cell(cid, variant, rep, fae.experiment.exp().smoke_workspaces)
    c.prepare(fresh=True)
    rc, _ = c.setup()
    if rc != 0:
        c.teardown()
        raise RuntimeError(f"{variant} setup failed (see {c.ws}/hooks.log)")
    try:
        lock = c.verify_lock_acquire()
        if lock is None:
            raise RuntimeError(f"{cid} stood down: .paused")
        try:
            return c.verify()
        finally:
            lock.close()
    finally:
        c.teardown()


def main(argv):
    """The reference benchmark: each sealed variant's reference through the full verify."""
    p = argparse.ArgumentParser(prog="cli.py experiment verb exp1", description=(
        "Run each variant's reference through the full verify --reps times on the "
        "canonical load shape; write results/exp1-<date>.md."))
    p.add_argument("--reps", type=int, default=3, help="runs per variant")
    p.add_argument("--variants", default=",".join(EXP1_VARIANTS),
                   help="comma-separated (default: the sealed trio)")
    p.add_argument("--report-only", action="store_true",
                   help="aggregate the existing reference workspaces; run nothing")
    return exp1(p.parse_args(argv))


def exp1(args):
    """Reference benchmark (the exp-1 comparison, on the consolidated rig):
    run each sealed arm's REFERENCE through the full verify N times on the
    canonical load shape; collect control-loop latency (metrics.json) and the
    peak resource footprint (resources.json, sampled during the k6 window);
    write results/exp1-<date>.md and print the table. No agent, no scoring —
    this measures the MECHANISMS against each other.

    Latency is reported on TWO bases, since one column would mix them: mount total = the first spike's saturation -> the
    infra-confirmed mount that starts in it, computable for every arm; provisioning = scaler decision -> mount,
    only where the decision is observable (sampler scaler_state, or the
    pulumi app-log markers) — '-' for external autoscalers like KEDA."""
    from fae.conduct import Conduct
    pid = None if args.report_only else Conduct().pid()
    if pid:
        print(f"WARNING: conduct is running (pid {pid}); each exp1 verify queues for the "
              f"verify lock behind its cells", flush=True)
    smoke_ws = fae.experiment.exp().smoke_workspaces
    rows = {}
    for arm in args.variants.split(","):
        runs = []
        for rep in range(1, args.reps + 1):
            cid = f"ref_high_smoke_{arm}_T1_r{rep}"
            if not args.report_only:
                print(f"== exp1 {arm} r{rep} ({cid})", flush=True)
                try:
                    _exp1_verify(cid, arm, rep)
                except (RuntimeError, OSError) as e:
                    print(f"   PREP FAIL: {str(e)[-200:]}")
                    continue
            c = fae.experiment.exp().cell(cid, workspaces=smoke_ws)
            met = c.read_metrics()
            try:
                res = json.loads(c.evidence("resources.json").read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"   NO DATA: {e}")
                continue
            if not met:
                print(f"   NO DATA: no metrics in {c.ws}")
                continue
            ok = bool(met.get("scaling_ok")) and met.get("e2e_green")
            # two-basis latency (see docstring): total for every arm,
            # provisioning only where the decision epoch is observable
            mount_total = prov = None
            try:
                ev = json.loads(c.evidence("trace_events.json").read_text())
                m_ep = ev.get("mount_epoch")
                on = ev.get("saturation_epoch")
                dec = ev.get("decision_epoch")
                if dec is None:
                    dec = pulumi_log_marks(c.evidence("service.deploy.log"))[0]
                mount_total = (first_spike_mount_s(c) if c.evidence("schedule.json").is_file()
                               else span(on, m_ep))
                prov = span(dec, m_ep)
            except (OSError, json.JSONDecodeError, ValueError):
                pass
            print(f"   {'GREEN' if ok else 'FAIL[' + str(met.get('stage_failed')) + ']'}"
                  f"  mount_total={mount_total}s prov={prov if prov is not None else '-'}s"
                  f"  peak_mem={res['peak']['total_mem_mb']}MB"
                  f"  peak_cpu={res['peak']['total_cpu_pct']}%"
                  f"  components={res['peak']['component_count']}")
            g = res["peak"].get("by_group", {})
            runs.append(dict(ok=ok, mount=mount_total, prov=prov,
                             app_mem=res["peak"].get("app_mem_mb"),
                             app_cpu=res["peak"].get("app_cpu_pct"),
                             total_mem=res["peak"]["total_mem_mb"],
                             total_cpu=res["peak"]["total_cpu_pct"],
                             comp=res["peak"]["component_count"],
                             avg_cores=res.get("avg_cores"),
                             errors=met.get("load_errors"),
                             groups=g))
        rows[arm] = runs

    def med(vals):
        vals = sorted(v for v in vals if v is not None)
        return vals[len(vals) // 2] if vals else None

    date = time.strftime("%Y%m%d")
    lines = [f"# exp1 — reference mechanism benchmark ({time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())})",
             "",
             f"Reps per arm: {args.reps}, canonical load shape, full verify each",
             "(store + calibrate + deploy + e2e + k6 + gate). Values = median",
             "(min-max) over green runs; footprint = peak during the k6 window.",
             "mount total = saturation onset -> infra-confirmed mount (same",
             "basis for every arm; includes the app's detection/sustain window);",
             "provisioning = scaler decision -> mount, only where the decision is",
             "observable ('-' = external autoscaler, decision inside the operator).",
             "Mount-total spread (the min-max) is dominated by the detection",
             "window's interaction with load noise — a policy/load property that",
             "is similar across arms, not a mechanism property; mechanism",
             "determinism shows in the provisioning spread. workload mem = the",
             "components the arm itself deploys/runs (docker+k8s+host planes);",
             "platform pods and the cluster node are separate columns so every",
             "observed MB is attributable.",
             "",
             "| arm | green | mount total s | provisioning s | workload mem MB | platform pods MB | cluster node MB | mean CPU cores | components | load errors |",
             "|---|---|---|---|---|---|---|---|---|---|"]

    def cell(g, key, fmt="{:.1f}"):
        vals = [r[key] for r in g if r.get(key) is not None]
        if not vals:
            return "-"
        m = med(vals)
        return (fmt.format(m) + f" ({fmt.format(min(vals))}-{fmt.format(max(vals))})"
                if len(vals) > 1 else fmt.format(m))

    def gmed(g, grp, metric, fmt="{:.1f}"):
        vals = [r["groups"].get(grp, {}).get(metric) for r in g]
        vals = [v for v in vals if v is not None]
        return fmt.format(med(vals)) if vals else "-"

    for arm, runs in rows.items():
        g = [r for r in runs if r["ok"]]
        lines.append(f"| {arm} | {len(g)}/{len(runs)} | {cell(g, 'mount')} "
                     f"| {cell(g, 'prov', '{:.2f}')} "
                     f"| {cell(g, 'app_mem')} | {gmed(g, 'infra_pods', 'mem_mb')} "
                     f"| {gmed(g, 'kind_node', 'mem_mb')} | {cell(g, 'avg_cores', '{:.2f}')} "
                     f"| {cell(g, 'comp', '{:.0f}')} | {cell(g, 'errors', '{:.0f}')} |")

    # detail: every observed group, per arm — the audit surface. GRAND total =
    # docker(all)+host (kind node contains its pods; pods are its breakdown).
    for metric, unit in (("mem_mb", "MB"), ("cpu_pct", "%")):
        pretty = "Memory" if metric == "mem_mb" else "CPU"
        tkey = "total_mem" if metric == "mem_mb" else None
        lines += ["", f"## {pretty} detail (median {unit} over green reps — every observed group)",
                  "",
                  "| arm | docker workload | k8s workload | host workload | workload total | platform pods | cluster node | machine total |",
                  "|---|---|---|---|---|---|---|---|"]
        for arm, runs in rows.items():
            g = [r for r in runs if r["ok"]]
            app_tot = cell(g, "app_mem") if metric == "mem_mb" else cell(g, "app_cpu")
            grand = cell(g, tkey) if tkey else cell(g, "total_cpu")
            lines.append(f"| {arm} | {gmed(g, 'docker_app', metric)} | {gmed(g, 'k8s_app', metric)} "
                         f"| {gmed(g, 'host_app', metric)} | {app_tot} "
                         f"| {gmed(g, 'infra_pods', metric)} | {gmed(g, 'kind_node', metric)} "
                         f"| {grand} |")
    lines += ["", "Attribution notes: platform pods = KEDA controller trio +",
              "metrics-server — mechanism cost for the keda arms, idle platform",
              "for pulumi (charge it to keda arms if comparing mechanisms).",
              "The cluster node contains the pods' memory (never re-added); machine",
              "total = all docker + host planes. Detail CPU % = of one",
              "core, at the peak-memory sample (phase-sensitive: the store's",
              "calibration/load burst lands in docker app for every arm)."]
    out = fae.experiment.exp().root / "results" / f"exp1-{date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines[10:]))
    print(f"\nwritten: {out}")
