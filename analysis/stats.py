"""Within-model design comparisons over the scored runs; writes the paper's
tables and macros as LaTeX into OUT_DIR (results, effects, checks, failures,
macros).

Usage: python3 -I analysis/stats.py WORKSPACES_DIR OUT_DIR [MAX_REP]

MAX_REP keeps only repetitions r1..MAX_REP of each arm (the paper uses 12).
Reference mount delays come from the smoke runs in ws-smoke.nosync/ beside
WORKSPACES_DIR, and the mount deadline from fae.toml there; without them the
corresponding macros read "n/a" and the experiment default of 30 s.
"""
import glob
import re
import json
import math
import os
import random
import sys
from collections import defaultdict

INF = float("inf")
DESIGNS = ("kedap", "kedam", "pulumi")
CONDS = ("sealed", "access")
PAIRS = (("kedap", "pulumi"), ("kedam", "pulumi"), ("kedap", "kedam"))
MEASURES = (("att", "iterations_to_green"), ("turns", "turns_total"), ("tok", "out_tok_total"))
MODELS = (
    ("haiku55", "Claude Haiku 5.5"),
    ("sonnet55", "Claude Sonnet 5.5"),
    ("opus55", "Claude Opus 5.5"),
    ("g38f", "Gemini 3.8 Flash"),
    ("g4h", "Gemini 4 Hydrogen"),
)
PAIR_TAG = {("kedap", "pulumi"): "K/Pu", ("kedam", "pulumi"): "M/Pu", ("kedap", "kedam"): "K/M"}
MIN_N = 3
MAX_REP = None
OUT_DIR = "."
DEFAULT_MOUNT_DEADLINE = "30"
MEAS_NAME = {"att": "attempts", "turns": "turns", "tok": "tokens"}
MEAS_TAG = {"att": "a", "turns": "t", "tok": "k"}
EXCLUDED = {}


def in_scope(cell_dir):
    cell = os.path.basename(cell_dir)
    m = re.search(r"_r(\d+)$", cell)
    return (m is not None and not cell.startswith("testagent") and cell not in EXCLUDED
            and (MAX_REP is None or int(m.group(1)) <= MAX_REP)
            and os.path.exists(os.path.join(cell_dir, "score.json")))


def load(ws):
    runs = defaultdict(list)
    for f in glob.glob(os.path.join(ws, "*", "score.json")):
        if not in_scope(os.path.dirname(f)):
            continue
        d = json.load(open(f))
        alias = d["cell_id"].split("_")[0]
        design, cond = d["variant"].replace("_apidocs", "").split("_")
        green = d["green"] and not d["revoked"]
        row = {"green": green}
        for m, key in MEASURES:
            row[m] = (d[key] if green else INF)
        runs[(alias, design, cond)].append(row)
    return runs


def u_stat(a, b):
    return sum(1 if x > y else 0.5 if x == y else 0 for x in a for y in b)


def perm_p(a, b):
    """Exact two-sided permutation p of the Mann-Whitney statistic (midranks for ties)."""
    pool = sorted(a + b)
    rank2 = {}
    i = 0
    while i < len(pool):
        j = i
        while j < len(pool) and pool[j] == pool[i]:
            j += 1
        rank2[pool[i]] = i + j + 1
        i = j
    k, n = len(a), len(a) + len(b)
    obs = abs(sum(rank2[x] for x in a) - k * (n + 1))
    ways = [defaultdict(int) for _ in range(k + 1)]
    ways[0][0] = 1
    for v in pool:
        r = rank2[v]
        for size in range(min(k, n) - 1, -1, -1):
            for total, count in list(ways[size].items()):
                ways[size + 1][total + r] += count
    extreme = sum(c for total, c in ways[k].items() if abs(total - k * (n + 1)) >= obs)
    return extreme / sum(ways[k].values())


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    out, run, m = [0.0] * len(ps), 0.0, len(ps)
    for r, i in enumerate(order):
        run = max(run, min(1.0, (m - r) * ps[i]))
        out[i] = run
    return out


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def vals(runs, alias, design, cond, m):
    return [r[m] for r in runs[(alias, design, cond)] if r[m] is not None]


def fmt(v, m):
    if v == INF:
        return "$\\infty$"
    if m == "tok":
        return f"{v / 1000:.0f}"
    return f"{v:g}" if m == "att" else f"{v:.0f}"


def draft_path(name):
    return os.path.join(OUT_DIR, name)


def write(name, lines):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(draft_path(name), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote", draft_path(name))


def cliffs_delta(a, b):
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return (gt - lt) / (len(a) * len(b))


def delta_ci(a, b, n=2000, rng=random.Random(2)):
    ds = sorted(cliffs_delta([rng.choice(a) for _ in a], [rng.choice(b) for _ in b]) for _ in range(n))
    return ds[int(0.025 * n)], ds[int(0.975 * n) - 1]


CHECKS = (
    ("never released", "Not released"),
    ("released too late", "Released late"),
    ("released MID-spike", "Released mid-spike"),
    ("outside spike", "Mount outside window"),
    ("mount episode+", "Too many mounts"),
    ("mount episode-", "Too few mounts"),
    ("load errors", "Requests failed"),
)
OTHER_STAGE = "Deploy or functional"


def check_of(metrics):
    if metrics.get("stage_failed") and metrics.get("stage_failed") != "scaling":
        return OTHER_STAGE
    first = str(metrics.get("scaling_why") or "").split(";")[0]
    m = re.search(r"expected (\d+) mount episode\(s\) \(one per spike\), saw (\d+)", first)
    if m:
        return "Too many mounts" if int(m.group(2)) > int(m.group(1)) else "Too few mounts"
    for key, label in CHECKS:
        if key in first:
            return label
    return "Other"


def failed_attempts(cell_dir):
    """The charged arrangement that decided each failed attempt of a run."""
    failed = set()
    for line in open(os.path.join(cell_dir, "iterations.log")):
        parts = line.rstrip().split("\t")
        if len(parts) > 3 and parts[1] == "ITER" and parts[2] == "fail":
            failed.add(int(re.search(r"attempt=(\d+)", parts[3]).group(1)))
    decided = {a: None for a in failed}
    for d in sorted(glob.glob(os.path.join(cell_dir, "arrangements", "*-charged"))):
        m = re.match(r"\d+-a(\d+)-", os.path.basename(d))
        if m and int(m.group(1)) in failed:
            decided[int(m.group(1))] = d
    return decided


def checks(ws):
    count = defaultdict(lambda: defaultdict(int))
    first = defaultdict(lambda: defaultdict(int))
    for cell_dir in glob.glob(os.path.join(ws, "*")):
        cell = os.path.basename(cell_dir)
        if not in_scope(cell_dir):
            continue
        design = cell.split("_")[2]
        for attempt, d in failed_attempts(cell_dir).items():
            mf = os.path.join(d, "metrics.json") if d else None
            label = check_of(json.load(open(mf))) if mf and os.path.exists(mf) else OTHER_STAGE
            count[label][design] += 1
            if attempt == 1:
                first[label][design] += 1
    labels = [label for _, label in CHECKS] + [OTHER_STAGE, "Other"]
    rows = [f"{label} & " + " & ".join(str(count[label][d]) for d in DESIGNS) + " \\\\"
            for label in labels if any(count[label][d] for d in DESIGNS)]
    rows.append("\\midrule")
    rows.append("All failed attempts & " + " & ".join(
        str(sum(count[l][d] for l in count)) for d in DESIGNS) + " \\\\")
    write("checks.tex", rows)
    print("first-attempt failures by check:", {l: dict(v) for l, v in first.items()})
    return count, first


def reference_mounts(ws):
    smoke = os.path.join(os.path.dirname(os.path.abspath(ws)), "ws-smoke.nosync")
    out = {}
    for d in DESIGNS:
        delays = [json.load(open(f)).get("mount_delay_s")
                  for f in glob.glob(os.path.join(smoke, f"ref_*_{d}_*", "arrangements", "*", "metrics.json"))]
        delays = [x for x in delays if x is not None]
        out[d] = (min(delays), max(delays)) if delays else (None, None)
    toml_path = os.path.join(os.path.dirname(os.path.abspath(ws)), "fae.toml")
    m = re.search(r'max_mount_s\s*=\s*"(\d+)"', open(toml_path).read()) if os.path.exists(toml_path) else None
    return out, (m.group(1) if m else DEFAULT_MOUNT_DEADLINE)


def cmh_p(strata):
    """Cochran-Mantel-Haenszel test (continuity-corrected) over 2x2 strata (pass_a, n_a, pass_b, n_b)."""
    num, var = 0.0, 0.0
    for xa, na, xb, nb in strata:
        n, m1 = na + nb, xa + xb
        if n < 2 or m1 in (0, n):
            continue
        num += xa - na * m1 / n
        var += na * nb * m1 * (n - m1) / (n * n * (n - 1))
    if var == 0:
        return 1.0
    chi = (abs(num) - min(0.5, abs(num))) ** 2 / var
    return math.erfc(math.sqrt(chi / 2))


def upper_bound(k, n, alpha=0.05):
    """Exact one-sided Clopper-Pearson upper bound of a binomial rate, k passes in n."""
    if k >= n:
        return 1.0
    cdf = lambda p: sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) > alpha else (lo, mid)
    return lo


def fmt_p(p):
    if p < 0.001:
        exp = math.floor(math.log10(p))
        return f"$<10^{{{exp + 1}}}$"
    return f"{p:.3f}"


def failures(ws):
    """Failed attempts per design, split by the verifier stage that failed."""
    count = defaultdict(lambda: defaultdict(int))
    for f in glob.glob(os.path.join(ws, "*", "iterations.log")):
        cell = os.path.basename(os.path.dirname(f))
        if not in_scope(os.path.dirname(f)):
            continue
        design = cell.split("_")[2]
        for line in open(f):
            parts = line.rstrip().split("\t")
            if len(parts) > 3 and parts[1] == "ITER" and parts[2] == "fail":
                stage = next((w[6:] for w in parts[3].split() if w.startswith("stage=")), "other")
                count[design]["all"] += 1
                count[design][stage if stage in ("scaling", "deploy") else "other"] += 1
    names = {"kedap": "P", "kedam": "M", "pulumi": "Pu"}
    lines = []
    for d in DESIGNS:
        for k in ("all", "scaling", "deploy", "other"):
            lines.append(f"\\newcommand{{\\fail{names[d]}{k}}}{{{count[d][k]}}}")
    total = {k: sum(count[d][k] for d in DESIGNS) for k in ("all", "scaling", "deploy", "other")}
    for k, v in total.items():
        lines.append(f"\\newcommand{{\\failAll{k}}}{{{v}}}")
    write("failures.tex", lines)


def main(ws):
    failures(ws)
    _, first_fail = checks(ws)
    runs = load(ws)
    lines, effects = [], []
    arm_n = {}
    for alias, name in MODELS:
        tests = []
        for m, _ in MEASURES:
            for cond in CONDS:
                for a, b in PAIRS:
                    xa, xb = vals(runs, alias, a, cond, m), vals(runs, alias, b, cond, m)
                    if len(xa) >= MIN_N and len(xb) >= MIN_N:
                        tests.append((m, cond, a, b, perm_p(xa, xb), xa, xb))
        adj = holm([t[4] for t in tests]) if tests else []
        sig = defaultdict(list)
        for t, h in zip(tests, adj):
            print(f"{alias:9} {t[1]:6} {t[0]:5} {t[2]}>{t[3]} p={t[4]:.4f} holm={h:.3f}")
            if h < 0.05:
                sig[t[1]].append(((t[2], t[3]), MEAS_TAG[t[0]]))
                lo, hi = delta_ci(t[5], t[6])
                effects.append(f"{name.replace('Claude ', '')} & {t[1]} & {PAIR_TAG[(t[2], t[3])]} & {MEAS_NAME[t[0]]} & "
                               f"{cliffs_delta(t[5], t[6]):.2f} & [{lo:.2f}, {hi:.2f}] & {'$<0.001$' if h < 0.001 else f'{h:.3f}'} \\\\")
        for i, cond in enumerate(CONDS):
            green = sum(r["green"] for d in DESIGNS for r in runs[(alias, d, cond)])
            total = sum(len(runs[(alias, d, cond)]) for d in DESIGNS)
            cells = []
            for d in DESIGNS:
                rs = runs[(alias, d, cond)]
                arm_n[(alias, d, cond)] = len(rs)
                cells.append(f"{sum(r['att'] == 1 for r in rs)}/{len(rs)}")
                for m, _ in MEASURES:
                    xs = vals(runs, alias, d, cond, m)
                    cells.append(fmt(median(xs), m) if len(xs) >= MIN_N else "--")
            by_pair = {}
            for pair, tag in sig[cond]:
                by_pair[pair] = by_pair.get(pair, "") + tag
            sigs = ", ".join(f"{PAIR_TAG[q]}:{by_pair[q]}" for q in PAIRS if q in by_pair) or "--"
            label = name if i == 0 else ""
            lines.append(f"{label} & {cond} & {green}/{total} & " + " & ".join(cells) + f" & {sigs} \\\\")
        lines.append("\\midrule" if alias != MODELS[-1][0] else "")
    write("results.tex", lines)
    write("effects.tex", effects or ["\\multicolumn{7}{c}{none} \\\\"])
    claude = [n for (a, _, _), n in arm_n.items() if a in ("haiku55", "sonnet55", "opus55")]
    gemini = [n for (a, _, _), n in arm_n.items() if a in ("g38f", "g4h")]
    mounts, deadline = reference_mounts(ws)
    names = {"kedap": "P", "kedam": "M", "pulumi": "Pu"}
    macros = [
        f"\\newcommand{{\\nRuns}}{{{sum(arm_n.values())}}}",
        f"\\newcommand{{\\nClaudeMin}}{{{min(claude)}}}",
        f"\\newcommand{{\\nClaudeMax}}{{{max(claude)}}}",
        f"\\newcommand{{\\nGeminiMin}}{{{min(gemini)}}}",
        f"\\newcommand{{\\nGeminiMax}}{{{max(gemini)}}}",
        f"\\newcommand{{\\mountDeadline}}{{{deadline}}}",
    ]
    for d in DESIGNS:
        rs = [r for (a, dd, _), lst in runs.items() if dd == d and a in dict(MODELS) for r in lst]
        macros.append(f"\\newcommand{{\\fp{names[d]}}}{{{sum(r['att'] == 1 for r in rs)}}}")
        macros.append(f"\\newcommand{{\\fp{names[d]}n}}{{{len(rs)}}}")
        lo, hi = mounts[d]
        span = f"{lo:g}--{hi:g}" if lo is not None else "n/a"
        macros.append(f"\\newcommand{{\\refMount{names[d]}}}{{{span}}}")
    fp_names = {"kedap": "P", "kedam": "M", "pulumi": "Pu"}
    for a, b in PAIRS:
        strata = []
        for alias, _ in MODELS:
            for cond in CONDS:
                ra, rb = runs[(alias, a, cond)], runs[(alias, b, cond)]
                strata.append((sum(r["att"] == 1 for r in ra), len(ra), sum(r["att"] == 1 for r in rb), len(rb)))
        pv = cmh_p(strata)
        print(f"CMH first-pass {a} vs {b}: p={pv:.2g}")
        macros.append(f"\\newcommand{{\\cmh{fp_names[a]}{fp_names[b]}}}{{{fmt_p(pv)}}}")
    kedap = [r for (al, d, _), lst in runs.items() if d == "kedap" and al in dict(MODELS) for r in lst]
    k_p = sum(r["att"] == 1 for r in kedap)
    macros.append(f"\\newcommand{{\\fpPupper}}{{{100 * upper_bound(k_p, len(kedap)):.1f}}}")
    top = max(first_fail, key=lambda l: first_fail[l]["kedap"])
    macros.append(f"\\newcommand{{\\fpPtopcheck}}{{{top.lower()}}}")
    macros.append(f"\\newcommand{{\\fpPtopn}}{{{first_fail[top]['kedap']}}}")
    macros.append(f"\\newcommand{{\\fpPfailn}}{{{sum(first_fail[l]['kedap'] for l in first_fail)}}}")
    write("macros.tex", macros)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    OUT_DIR = sys.argv[2]
    if len(sys.argv) > 3:
        MAX_REP = int(sys.argv[3])
    main(sys.argv[1])
