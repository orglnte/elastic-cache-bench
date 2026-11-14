# experiment/scoring/rubric.md — how to score a cell

One score record per cell. Some fields are **auto-computed** by
`score_cell.py` from the workspace; others require **human grading** and are read
from a per-cell `grading.json` (fill it from `grading-template.json`). Keep grading
mechanical and, where possible, blind to treatment.

## Fields and who produces them

| field | metric (DESIGN §8) | source |
|-------|--------------------|--------|
| `author_surface.files` / `.languages` / `.lines` | 4 (secondary) | auto — counted from `workspaces/<cell>/artifacts/` |
| `iterations_to_green`, `green`, `budget_exhausted` | 3 | auto — parsed from `iterations.log` (override allowed in `grading.json`) |
| `doc_lines` | matched-doc check (§4) | auto — from `cell.env` (0 if cold) |
| `first_pass_correct`, `correctness_tier`, `acceptance_met` | 2 | **human** — `grading.json` |

## Metric 2 — first-pass correctness (human static + auto live)

Judge the agent's **first "done" submission**, before any human edit.

- **Static (human, `grading.json`):** `correctness_tier = "static"` — required
  artifacts present + internally consistent + would-deploy. `first_pass_correct`
  is `true` only if **all** `AC*` are met with no human fix; record them in
  `acceptance_met`.
- **Live (AUTO, `verify.sh` → `metrics.json`, merged by `score_cell.py`):**
  `deploy_ok`, `e2e_pass`/`e2e_total`, `e2e_green` (the live first-pass signal),
  and `load_errors`/`load_total` measured **only on the e2e-green build**. You do
  not hand-code these — they come from the treatment-agnostic verifier.
  `verify_stage_failed` localizes any non-green (deploy/readiness/e2e/k6).

## Metric 3 — iterations-to-green (proxy)

Auto-parsed from `iterations.log`: number of build→`verify.sh` cycles up to and
including the first `green`, capped at `ATTEMPT_BUDGET` (**5**). Cells not green
by 5 are censored `budget` (`budget_exhausted`). If your agent self-reports its
own cycle count, `grading.json.iterations_to_green` overrides the parse. Name it a
proxy in any writeup.

## Metric 4 — author surface (secondary proxy)

Auto-counted from `artifacts/`. Because the fixed skeleton is seeded into every
cell, the headline `files`/`languages`/`lines` are the **agent-authored** surface
(files new or modified vs the seeded `.skeleton_manifest`); `total_*` keep the
full surface for context. Proxy for "easier"; report as secondary.
