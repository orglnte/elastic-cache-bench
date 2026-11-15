# Artifacts

The benchmark is three artifacts: two code repositories and one dataset.

| Artifact | What it holds | Link |
|---|---|---|
| Benchmark (this repository) | the task, the service skeleton, the six variants and their reference implementations, the verifier and its load law parameters, the grading rules, the tests, the data card | https://anonymous.4open.science/r/elastic-cache-bench-80F6/ |
| Engine | the harness the benchmark runs on: cells (authoring attempts in containers, checkpoints, the attempt ledger), the scheduler, remote verify hosts and their calibration, the elastic-resource law, scoring | https://anonymous.4open.science/r/fae-7DD9/ |
| Dataset | the recorded runs: per run the ledger, the agent transcripts and the final tree (every attempt's tree for the runs after 14 November 2026, see `DATA_CARD.md`), and per arrangement the verdict, schedule, per-second trace, load results and logs | https://doi.org/10.6084/m9.figshare.35014027 |

## How they fit together

1. The benchmark imports the engine from a checkout next to it (`../fae`, or
   the path in `$FAE_DIR`); nothing is installed. `README.md` and `AGENTS.md`
   here say how to set it up, check it and run it; the engine's `README.md`
   is its runbook.
2. The dataset is what `python3 cli.py experiment run` writes under
   `workspaces.nosync/`, one directory per run. `DATA_CARD.md` describes every
   file and field, how a verdict is reached, the conditions that varied
   during collection, and what was left out of the release.
3. Scores are recomputed from the dataset with `python3 cli.py results score`,
   which writes one `score.json` per run and `results.csv`; the fields are in
   the engine's `fae/experiment/scoring/SCHEMA.md`.
4. The paper's tables are recomputed from the scored runs with
   `python3 -I analysis/stats.py workspaces.nosync OUT_DIR 12`, which writes
   them as LaTeX into `OUT_DIR`: the within-model permutation tests with Holm
   correction, Cliff's delta, the Cochran-Mantel-Haenszel test of first-attempt
   passes, and the failed attempts by check, over repetitions 1 to 12 of each
   arm.
5. Each start of the scheduler is recorded in the dataset's
   `.conduct/regimes.jsonl` with the engine and benchmark commits, the
   concurrency caps and the CPU split it ran with.
