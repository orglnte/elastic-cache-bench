"""The experiment's taint rules: what makes a terminal cell's verdict
untrustworthy — a rig fault the rig itself could not see — as opposed to a
failure of the authoring. The engine's validator (fae/experiment/scoring/validate.py) runs
`rules` on every DONE cell and records the result in validation.json; the
engine keeps only the rules that are its own (a provider wall charged as an
attempt, a reverify aborted on a rig fault, an unreliable footprint).

Junction findings (keda APP_HOST rewrites, ClusterIP collisions, pulumi ghost
state, a store that did not come up charged as a build) are the rig-suspect
rules: a defect in the rig, not the agent's authoring, must not count
against a variant.
"""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path

import fae.experiment


def _parse(cid):
    from fae.experiment import parse_cell_id
    return parse_cell_id(cid)


def _cell(d):
    """The cell whose folder is `d`."""
    p = Path(d)
    return fae.experiment.exp().cell(p.name, workspaces=p.parent)


def _arm_of(variant):
    """The variants that share this one's tech and access factors (its arm):
    the cells whose evidence speaks for the same infra path."""
    d = fae.experiment.exp().definition
    cls = d.variant(variant)
    if cls is None:
        return {variant}
    key = (cls.FACTORS.get("tech"), cls.FACTORS.get("access"))
    return {v for v, c in d.variants.items()
            if (c.FACTORS.get("tech"), c.FACTORS.get("access")) == key} or {variant}


def _cells_of_arm(workspace, variant):
    arm = _arm_of(variant)
    for cid in workspace.cells():
        p = _parse(cid)
        if p and p[1] in arm:
            yield workspace.cell(cid)


# scaling_why clauses that mean the MOUNT BEHAVIOUR itself was wrong — as
# opposed to the load-error clause, which is usually downstream of it.
_MOUNT_COUNT_RE = re.compile(r"expected (\d+) mount episode\(s\).*?saw (\d+)")
_MOUNT_TIMING_RE = re.compile(r"released MID-spike|outside spike")

def mount_counts_seen(rc_text):
    """Every 'saw N' mount-episode count the scaling judge reported, in order.

    One entry per judged attempt that reached the scaling stage. Attempts that
    never got that far (build/e2e failures, HALTs) contribute nothing.
    """
    return [int(m.group(2)) for m in _MOUNT_COUNT_RE.finditer(rc_text)]


def host_sleep_gap_s(cell):
    """The longest wall-clock gap between consecutive sidecar ticks in any
    surviving trace of the cell — the mark a host suspend leaves inside a
    load window. 0 when every tick is within the threshold."""
    worst = 0
    for f in [cell.evidence("trace.csv"), *cell.judged("trace.csv")]:
        try:
            rows = list(csv.DictReader(f.open()))
            ts = [float(r["epoch"]) for r in rows if r.get("epoch")]
        except (OSError, ValueError, KeyError):
            continue
        for a, b in zip(ts, ts[1:]):
            worst = max(worst, b - a)
    return int(worst) if worst > cell.SLEEP_GAP_S else 0


def mount_report_text(cell):
    """The scaling judge's reports for every attempt of a cell, wherever the
    driver put them. A bash-era cell teed its whole console into run_cell.log
    (complete, every attempt). A py cell has no console log: its reports live
    in the per-arrangement verify.log archive, with the top-level verify.log
    a copy of the last — read the archive, and the top-level only when no
    archive exists."""
    if cell.evidence("run_cell.log").exists():
        return cell.evidence_text("run_cell.log")
    parts = [f.read_text(errors="replace") for f in cell.judged("verify.log")]
    if not parts and cell.evidence("verify.log").exists():
        parts.append(cell.evidence_text("verify.log"))
    return "\n".join(parts)


def never_mounted(rc_text):
    """True when the cell reached the scaling stage repeatedly and NEVER once
    mounted the cache.

    Any model can get a cache mounted somehow; the hard part of this task is
    mounting at the right time. A cell that reaches the scaling judge again and
    again and never mounts is more likely a rig fault (e.g. a provisioning
    port that never becomes reachable) than an authoring failure, so it is
    raised rather than scored as a fail.

    Requires >=2 scaling-stage attempts: a single `saw 0` is ordinary (a first
    attempt often misses entirely), whereas an unbroken run of them is not.
    """
    counts = mount_counts_seen(rc_text)
    return len(counts) >= 2 and max(counts) == 0


def arm_ever_served_clean(workspace, variant, thresh=0.05):
    """Has ANY cell of this variant's arm mounted correctly AND served the
    load without errors?

    The companion to arm_ever_mounted, for the errors-only case: a cell
    whose mounts matched in count and timing but which still returned errors.
    That reads as unexplained — until a sibling proves the rig can serve
    cleanly, at which point the errors are the policy's own, and the scaling
    judge already says so ("the policy must serve every request"): mounting at
    the right moment but dropping requests while doing it is an authoring
    failure, not a rig fault.

    A sibling that served cleanly on the same rig proves the rig can serve.
    """
    for c in _cells_of_arm(workspace, variant):
        if not c.env:
            continue
        m = c.read_metrics()
        if not m:
            continue
        lt, le = m.get("load_total") or 0, m.get("load_errors") or 0
        if lt and mounts_matched(m) and le / lt <= thresh:
            return True
    return False


def arm_ever_mounted(workspace, variant):
    """Has ANY cell of this variant's arm ever got a cache mounted?

    This is what separates "the rig cannot mount" from "this model cannot
    mount", and only the former is a rig fault. A green proves a mount
    outright; a `saw N>0` proves the provisioning path works even on a cell
    that failed the timing.

    Without it the never-mounted rule would flag every cell of a model that
    never mounts anywhere; a variant on which no cell ever mounted is the
    signature of a rig fault.
    """
    for c in _cells_of_arm(workspace, variant):
        if not c.env:
            continue
        if c.read_ledger()["verdict"] == "green":
            return True
        counts = mount_counts_seen(mount_report_text(c))
        if counts and max(counts) > 0:
            return True
    return False


def mounts_matched(metrics):
    """Did the cache mount the RIGHT NUMBER of times, at the RIGHT TIMES?

    This is the gate for whether a high load-error rate is meaningful. If the
    policy never mounted (or mounted at the wrong moment), saturating the
    capped store and timing out is the EXPECTED consequence — the errors are
    the failure being measured, not evidence of one. Only when the mount
    episodes match in both count and timing is a high error rate anomalous,
    because then the policy did the right thing and requests still failed.

    Returns True when scaling_why reports no count mismatch and no timing
    violation — which includes every green cell (why is None), and also the
    interesting case of a cell that mounted correctly and failed ONLY on load
    errors. That case is not green, so a green-only gate would miss it.
    """
    if metrics.get("scaling_ok") is True:
        return True
    why = metrics.get("scaling_why")
    if not why:
        # no scaling verdict recorded: nothing asserts the mounts were right
        return False
    if _MOUNT_TIMING_RE.search(why):
        return False
    m = _MOUNT_COUNT_RE.search(why)
    if m and m.group(1) != m.group(2):
        return False
    return True


# Rules 12-16: defects at the seam between the rig and one arm's framework,
# found by the 2026-08-27 corpus scan and fixed in the rig the same day. The
# affected cells are never re-run (bash era) — they are tainted here so the
# scoreboard excludes them and says so. Each rule matches the defect's own
# signature in the rig-written logs; rules 14 and 16 need a date cutoff, since
# their signal (a Deployment not named redis; a ghost container) is not
# specific to the old rig.


JUNCTION_FIX_CUTOFF = "2026-08-27T08:30:00Z"   # last commit of the fixes landed


_OLD_APP_HOST_NOTE = "NOTE: no http://<host>:"

_APP_HOST_TARGET_RE = re.compile(r"(?:^|[^A-Za-z0-9_])APP_HOST(?:[^A-Za-z0-9_]|$)")


_CLUSTERIP_COLLISION = "provided IP is already allocated"


_GHOST_STATE = ("No such container", "stack is currently locked")


def _sealed_before(cell, cutoff):
    at = cell.sealed_at
    return bool(at) and at < cutoff


def _last_deploy_log(cell):
    arr = cell.judged("deploy.log")
    f = arr[-1] if arr else cell.evidence("deploy.log")
    return f.read_text(errors="replace") if f.exists() else ""


def _placeholder_scrape_targets(cell):
    """The scrape URLs the cell's last Prometheus reported that still name
    the `APP_HOST` placeholder — the scraper's own account of a target the
    bring-up never rewrote. The diag is captured after the failed verify:
    the root copy (one per cell) or the last arrangement's."""
    arr = cell.judged("cluster-diag/prom-targets.json")
    f = arr[-1] if arr else cell.evidence("cluster-diag/prom-targets.json")
    try:
        targets = json.loads(f.read_text())["data"]["activeTargets"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return sorted({t.get("scrapeUrl", "") for t in targets
                   if _APP_HOST_TARGET_RE.search(t.get("scrapeUrl", "") or "")})


def _k8s_deployment_names(cell):
    names = set()
    k8s = cell.artifacts / "k8s"
    for y in list(k8s.glob("*.yaml")) + list(k8s.glob("*.yml")):
        for doc in y.read_text(errors="replace").split("\n---"):
            if re.search(r"^kind:\s*Deployment\s*$", doc, re.M):
                m = re.search(r"^metadata:\s*\n(?:\s+.*\n)*?\s+name:\s*(\S+)", doc, re.M)
                if m:
                    names.add(m.group(1).strip('"\''))
    return names


def _tech_of(variant):
    """The variant's tech, declared in its factors."""
    cls = fae.experiment.exp().definition.variant(variant)
    return cls.FACTORS.get("tech", "") if cls is not None else ""


def junction_findings(cell, it_text, green):
    """(taints, warns) for rules 12-16. A cell that went green despite the
    defect keeps its verdict and gets a warn: its attempt count is inflated,
    and what to do with that is the operator's call."""
    parsed = _parse(cell.cid)
    tech = _tech_of(parsed[1]) if parsed else ""
    taints, warns = [], []

    # 12 — the rig's own store failed to come up and the attempt was charged
    # (the bash driver; the python driver voids stage=store).
    charged = [int(m.group(1)) for m in
               re.finditer(r"\tITER\t(?:fail|budget)\tattempt=(\d+) stage=store", it_text)]
    if charged:
        taints.append(f"rule 12: attempt(s) {','.join(map(str, charged))} charged on "
                      f"FAIL[store] — the rig's capped store did not come up; a rig "
                      f"fault the bash driver charged as a build")

    if tech == "keda":
        # 13 — APP_HOST left un-rewritten: the bring-up honoured only the URL
        # form of the placeholder, so the scrape pipeline never reached the
        # app. The old NOTE line is the signature; the fixed bring-up's NOTE
        # is worded differently.
        last = _last_deploy_log(cell)
        status = [l for l in last.splitlines()
                  if _OLD_APP_HOST_NOTE in l or "rewrote app scrape target" in l]
        if status and _OLD_APP_HOST_NOTE in status[-1]:
            if not green:
                taints.append("rule 13: APP_HOST scrape target never rewritten on the "
                              "last attempt — the bring-up honoured only the URL form "
                              "of the placeholder, so no metric ever reached the "
                              "autoscaler")
        elif any(_OLD_APP_HOST_NOTE in t for t in cell.evidence_all("deploy.log")):
            warns.append("rule 13: APP_HOST left un-rewritten on an earlier attempt "
                         "(rig omission) — attempts-to-green inflated")
        # The NOTE names only the file the old bring-up looked in; a scrape
        # config in any other file got no rewrite and no NOTE. The scraper
        # itself reports the target it was left with.
        urls = _placeholder_scrape_targets(cell)
        if urls:
            msg = (f"rule 13: the scraper's last state still targets "
                   f"{','.join(urls)} — the bring-up rewrote APP_HOST in one "
                   f"file only, so no metric ever reached the autoscaler")
            if green:
                warns.append(msg + "; went green regardless — attempts-to-green inflated")
            else:
                taints.append(msg)
        # 14 — the cache Deployment carried a name the mount probe could not
        # see (it required a Deployment literally named `redis`).
        if not green and _sealed_before(cell, JUNCTION_FIX_CUTOFF):
            names = _k8s_deployment_names(cell)
            cacheish = sorted(n for n in names if re.search(r"(^|[-_])(redis|cache)", n, re.I))
            if names and "redis" not in names and cacheish:
                taints.append(f"rule 14: cache Deployment {','.join(cacheish)} not named "
                              f"`redis` — the mount probe of the rig that verified this "
                              f"cell could not see it, so no mount could be counted")
        # 15 — a pinned ClusterIP collided with the namespace reset's
        # asynchronous IP release; the deploy was charged as an authoring
        # rejection.
        hits = sum(t.count(_CLUSTERIP_COLLISION) for t in cell.evidence_all("deploy.log"))
        if hits:
            charged_deploys = len(re.findall(r"\tITER\t(?:fail|budget)\tattempt=\d+ stage=deploy", it_text))
            if not green:
                taints.append(f"rule 15: {hits} deploy(s) rejected on a ClusterIP collision "
                              f"after the namespace reset ({charged_deploys} attempt(s) "
                              f"charged) — a rig race, not an authoring error")
            else:
                warns.append(f"rule 15: {hits} deploy(s) rejected on a ClusterIP collision "
                             f"({charged_deploys} attempt(s) charged) — attempts-to-green "
                             f"inflated; keep or annotate is the operator's call")

    if tech == "pulumi" and _sealed_before(cell, JUNCTION_FIX_CUTOFF):
        # 16 — the Pulumi state (and its lock) survived the per-arrangement
        # wipe: the program acted on containers the rig had removed.
        logs = cell.evidence_all("service.deploy.log")
        sig = [s for s in _GHOST_STATE if any(s in t for t in logs)]
        if sig:
            msg = (f"rule 16: pulumi state survived the per-arrangement wipe "
                   f"({'; '.join(sig)}) — the program acted on containers the rig "
                   f"had removed")
            if not green:
                taints.append(msg)
            else:
                warns.append(msg + "; went green regardless — attempts-to-green inflated")
    return taints, warns




def rules(cell, workspace, metrics, it_text, v_text, rc_text, verdict):
    """(taints, warns, fields) for one DONE cell; `workspace` holds its
    siblings. `fields` are recorded in validation.json beside the verdict."""
    taints, warns = [], []
    min_qps = int(os.environ.get("CALIBRATION_MIN_QPS", 40))
    # ONE ceiling is recorded per cell (metrics.json carries the deciding
    # verify's, overwritten by each verify), so `deciding` holds at most one
    # element. Two consequences that made this block claim more than it did:
    #
    #   * `min(deciding) < 0.6 * max(deciding)` is `x < 0.6x` for a
    #     single-element list — FALSE for every positive ceiling. The
    #     "ceiling fluctuated ... (unstable host)" taint could never fire, so
    #     the host-instability rule the validator advertises did not exist,
    #     while every validation.json still recorded rule_set 2 as if it had
    #     been applied. Removed rather than left as a rule that cannot run:
    #     detecting an unstable host needs a per-attempt ceiling HISTORY, which
    #     nothing collects today. Recorded in the open list instead of
    #     pretending.
    #   * `hist = deciding` was an ALIAS, not a separate per-attempt history,
    #     so the "some attempt measured Nqps" warn fired on exactly the same
    #     condition as the taint two lines above it — every low-ceiling cell
    #     got the same finding twice, once as a taint and once as a warn.
    deciding = [int(metrics["ceiling_qps"])] if metrics.get("ceiling_qps") else []
    if deciding and deciding[-1] < min_qps:
        taints.append(f"deciding ceiling {deciding[-1]}qps < {min_qps}")
    if "HOST OVERLOADED" in v_text:
        warns.append("host-overload guard fired during the deciding verify "
                     "(slept + re-benched before proceeding)")

    # load delivery: a scaling verdict judged on undelivered load is worthless
    lt, le = metrics.get("load_total") or 0, metrics.get("load_errors") or 0
    # Only when load was DUE. The e2e_green conjunct is for metrics.json files
    # that predate null semantics: they record false for any cell that died
    # pre-e2e, and tainting those would reclassify hard failures as rig faults.
    if metrics.get("k6_available") is False and metrics.get("e2e_green"):
        taints.append("k6 unavailable — load never ran as designed")
    elif lt and le / lt > 0.05:
        # Count the errors ONLY when the mount episodes matched in count and
        # timing. If the policy never mounted (or mounted at the wrong
        # moment), every read hits the capped store, it saturates, and
        # requests hit the client timeout wall — the errors ARE the failure
        # being measured, and the scaling judge already accounts for them
        # ("the policy must serve every request"). Tainting there flags
        # exactly the cells that fail hardest, which the study wants to keep.
        # Evidence (2026-07-29): 9 cells raised as TAINTED were all genuine
        # failures. haiku_high_pulumi_sealed_onlysrc_T1_r1 recorded
        # "expected 2 mount episode(s), saw 0" with errors on EVERY attempt
        # (18-79%), while other cells on the same host the same day served
        # 6253 requests at 0.00%. Host contention cannot produce that split.
        # When the mounts WERE right and requests still failed, nothing
        # explains the errors — that is a real anomaly (rig, host or a
        # genuinely broken read path) and is raised.
        _p = _parse(cell.cid)
        _tr = _p[1] if _p else ""
        if mounts_matched(metrics) and not arm_ever_served_clean(workspace, _tr):
            taints.append(f"load error rate {le}/{lt} > 5% despite mount "
                          f"episodes matching in count and timing, and NO cell "
                          f"of {_tr} has ever mounted AND served cleanly — "
                          f"errors are unexplained by the scaling policy")
        elif mounts_matched(metrics):
            warns.append(f"load error rate {le}/{lt} > 5% with mounts matching "
                         f"— other {_tr} cells mount AND serve cleanly, so the "
                         f"policy is dropping requests across the transition "
                         f"(authoring, not rig)")
        else:
            warns.append(f"load error rate {le}/{lt} > 5% — expected when the "
                         f"policy fails to mount (not a rig fault)")

    # NEVER mounted, across every scaling-stage attempt => suspect the RIG.
    # See never_mounted(): mounting at all is the easy part, so an unbroken run
    # of `saw 0` is a provisioning-path failure far more often than an
    # authoring one. Kept as an explicit, separately-tracked failure mode so
    # the next occurrence is caught by the validator instead of by hand.
    # A GREEN cell demonstrably mounted: the gate cannot pass otherwise. Its
    # `saw 0` lines come from the attempts it failed BEFORE greening, and a
    # successful attempt logs no FAIL[scaling] line at all — so the counts only
    # ever contain failures and would read as "never mounted" for every cell
    # that struggled and then succeeded.
    if verdict != "green" and never_mounted(rc_text):
        n = len(mount_counts_seen(rc_text))
        parsed = _parse(cell.cid)
        variant = parsed[1] if parsed else ""
        if variant and not arm_ever_mounted(workspace, variant):
            taints.append(
                f"cache NEVER mounted in {n} scaling attempt(s), and NO cell of "
                f"{variant}'s arm has ever mounted one — suspect the provisioning "
                f"path, not the agent: check the provisioning in service.deploy.log and "
                f"that the readiness port matches the sidecar's published port")
        else:
            warns.append(f"cache never mounted in {n} scaling attempt(s) — other "
                         f"{variant}-arm cells DO mount, so this is an authoring "
                         f"failure rather than a rig fault")
    lr = metrics.get("load_ran")
    # load_ran is a bool in current metrics.json (did the load phase run);
    # only a numeric value supports a delivered-fraction check
    if isinstance(lr, (int, float)) and not isinstance(lr, bool) \
            and lt and lr / lt < 0.95:
        taints.append(f"only {lr}/{lt} of the designed load ran")
    elif lr is False:
        # NOT a taint (operator decision 2026-07-30). The verify
        # (fae/cell/verify.py) initialises load_ran=False and writes
        # metrics.json from its `finally` on EVERY path, so any cell that
        # fails before the k6 stage — a build
        # that never deploys, a failed precheck, a broken e2e — records
        # load_ran: false. Tainting that means "do not believe this verdict",
        # which pulls it from the dataset: and those are the cells that failed
        # HARDEST, the ones that could not get a deploy up at all. Removing the
        # worst authoring failures while keeping every cell that survived long
        # enough to be load-tested biases the result toward the hypothesis.
        #
        # Never reaching load is a RESULT, not a reason to distrust the rig. It
        # stays a plain failure, and is recorded as a warn so the reader knows
        # the scaling verdict rests on no delivered load.
        warns.append("load phase never ran — the cell failed before the load "
                     "stage, so there is no delivered-load evidence behind its "
                     "scaling verdict (a failure, not a rig fault)")

    if metrics.get("e2e_green") and (metrics.get("e2e_pass") or 0) < (metrics.get("e2e_total") or 0):
        taints.append(f"e2e_green with e2e {metrics.get('e2e_pass')}/"
                      f"{metrics.get('e2e_total')} (inconsistent metrics)")
    # e2e_green + stage_failed=scaling is COHERENT (e2e passed, then the
    # scaling law failed — a normal censor). Inconsistent only when the
    # failed stage precedes e2e, so e2e could not have passed (rule_set 2:
    # rule_set 1 false-tainted every scaling censor).
    if metrics.get("e2e_green") and metrics.get("stage_failed") in (
            "deploy", "precheck", "readiness", "e2e"):
        taints.append(f"e2e_green with stage_failed="
                      f"{metrics.get('stage_failed')} (pre-e2e stage)")

    slept = host_sleep_gap_s(cell)
    if slept:
        # a suspend inside a load window: that attempt's errors were the
        # host's, so a FAIL there may be false — a green never is
        warns.append(f"host-sleep gap {slept}s inside a load window — a false "
                     f"fail is possible on that attempt, never a false green")
    jt, jw = junction_findings(cell, it_text, verdict == "green")
    taints += jt
    warns += jw
    return taints, warns, {"ceilings_deciding": deciding, "host_sleep_gap_s": slept}


def selftest(workspaces):
    """Failures, as strings, of the pins this experiment keeps on its own
    rules — run by `cli.py rig selftest` through the definition's verbs."""
    fails = []
    # mounts_matched gates whether a high load-error rate counts as a taint,
    # so a parser that silently stops recognising a scaling_why clause would
    # turn every hard failure back into a false TAINTED (or, worse, hide a
    # real anomaly). Pin it to strings taken verbatim from real metrics.json.
    for why, ok, note in (
        (None, True, "scaling_ok=True, no why recorded"),
        ("expected 2 mount episode(s) (one per spike), saw 0", False,
         "count mismatch"),
        ("expected 2 mount episode(s) (one per spike), saw 2", True,
         "count matches, no timing complaint"),
        ("expected 2 mount episode(s) (one per spike), saw 3; episode 1 "
         "released MID-spike (ts=28 < spike end 46)", False, "count + timing"),
        ("expected 2 mount episode(s) (one per spike), saw 2; mount 2 at "
         "ts=41 outside spike 2's window [79,95]", False, "timing only"),
        ("expected 2 mount episode(s) (one per spike), saw 0; load errors "
         "1179/8049 = 14.6% > 0% (the policy must serve every request)",
         False, "count mismatch reported alongside errors"),
        ("load errors 900/8000 = 11.2% > 0% (the policy must serve every "
         "request)", True, "errors only, mounts never faulted -> anomaly"),
    ):
        m = {"scaling_ok": True} if why is None else {"scaling_ok": False,
                                                      "scaling_why": why}
        if mounts_matched(m) is not ok:
            fails.append(f"mounts_matched({note}): expected {ok}")
    # never_mounted is the rig-fault detector for the provisioning path; pin it
    # so the "saw 0 forever" signature can never silently stop being noticed.
    for text, ok, note in (
        ("", False, "no scaling attempts at all"),
        ("expected 2 mount episode(s) (one per spike), saw 0", False,
         "a single saw-0 is ordinary, not yet a pattern"),
        ("expected 2 mount episode(s), saw 0\nexpected 2 mount episode(s), saw 0",
         True, "two saw-0 in a row => suspect the rig"),
        ("expected 2 mount episode(s), saw 0\nexpected 2 mount episode(s), saw 1",
         False, "mounted once somewhere => authoring failure, not rig"),
        ("expected 2 mount episode(s), saw 3\nexpected 2 mount episode(s), saw 2",
         False, "mounts happening, wrong count"),
    ):
        if never_mounted(text) is not ok:
            fails.append(f"never_mounted({note}): expected {ok}")
    # GREEN => the mounts matched in count and timing. The gate only mints a
    # green when the scaling law held, so a green whose own metrics report a
    # mount fault means either the gate minted a bad green or mounts_matched
    # has regressed. Checked across the whole corpus, so a rule change is
    # re-applied retroactively.
    for d in sorted(Path(workspaces).iterdir()) if Path(workspaces).is_dir() else []:
        if not d.is_dir() or not _parse(d.name):
            continue
        c = _cell(d)
        if not c.has_ledger:
            continue
        met = c.read_metrics()
        if met and c.read_ledger()["verdict"] == "green" and not mounts_matched(met):
            fails.append(f"{d.name}: GREEN but mounts_matched=False "
                         f"(scaling_ok={met.get('scaling_ok')} "
                         f"why={str(met.get('scaling_why'))[:70]})")
    return fails
