# Data card: elastic-cache-bench runs

## What a record is

The data is a set of **runs**. A run is one coding agent (one model) authoring
one variant of the task once (`<agent>_<effort>_<variant>_T1_r<rep>`). A run
is a sequence of **attempts** (at most 10): the agent edits the service, then
the verifier judges the build on a **gate** of six **arrangements**. An
attempt is green when all six pass; the run ends at its first green attempt
or when the budget is spent. Each arrangement is one deploy → load → teardown
cycle on a dedicated verify host, under one load schedule.

| Level | Unit | Where |
|---|---|---|
| run | one agent × variant × repetition | `workspaces.nosync/<run>/` |
| attempt | one authoring pass and its gate | ledger lines `attempt=N`; `agent.attempt-N.log` |
| arrangement | one load schedule, one verdict | `<run>/arrangements/<NN>-a<N>-<SHAPE>-<outcome>/` |

**Variants** (six): the provisioning framework, `kedap` (KEDA, `prometheus`
scaler), `kedam` (KEDA, `metrics-api` scaler) or `pulumi`, crossed with
`sealed` (the agent has no cluster to try its work on) or `access` (it has a
sandbox cluster). All use the `apidocs` documentation level. The task, the
service skeleton and the verifier are the same for every variant
(`experiment/README.md`).

**Arrangements**: `BBS`, `BSB`, `SBB`, `SSB`, `SBS`, `BSS`, a sequence of
three load events, `B` a blip (a short burst that must not mount the cache)
and `S` a spike (sustained load that must mount it within 30 s of the store
saturating, and release it after the traffic ends).

**Models**: `opus55`, `sonnet55`, `haiku55` (Claude Opus/Sonnet/Haiku 5.5,
through Claude Code), `g38f` (Gemini 3.8 Flash) and `g4h` (Gemini 4
Hydrogen), through the `agy` CLI. The Gemini models have fewer repetitions
per variant than the Claude models. The exact model id of each run is in its
`cell.env` and `score.json`.

**Counts**: one row per run in `results.csv`, written by
`python3 cli.py results score` from the run directories.

## How a verdict is reached

Per arrangement the verifier deploys the agent's build, runs the functional
tests (7), then the load schedule (`schedule.json`) with k6 while a sidecar
samples the service once per second (`trace.csv`). The engine's law
(`fae/cell/contrib/elastic_resource/law.py`, documented in the engine's
AGENTS.md §7) reads the trace against the schedule and decides one of:

| Outcome | Meaning | Counted |
|---|---|---|
| `green` | the arrangement passed | yes |
| `charged` | the build failed it (`stage`: `scaling`, `deploy`, `e2e`, …); the attempt fails | yes |
| `refunded` | the run could not judge the build (`stage`: `load-shape`, `store`, `host-sleep`, `verifier-host`, …): the arrangement is void and runs again | no |
| `interrupted` | the verify was stopped before a verdict | no |

An attempt stops at its first charged arrangement, so later arrangements of
that attempt have no record. `stage` and `why` in `verdict.json` name the
failure in words (e.g. `released too late`, `expected 1 mount episode(s)`,
load errors with their count).

## Files of a run

| File | Content |
|---|---|
| `cell.env` | the run's coordinates: id, task, variant, repetition, attempt budget, model id |
| `iterations.log` | the ledger, one tab-separated line per event: `PREPARED`, `START` / `END`, `AGENT` (agent seconds, client and its version), `CKPT` (git tree before and after the attempt), `VERIFY_READY`, `SHAPE` (one per arrangement), `ITER` (attempt verdict, `verify_s`, tree), `HEAL` (files moved back out of the agent's surface), `RESTORE`, `PAUSED`, `HALT`, `ALERT` |
| `agent.attempt-N.log` | the agent client's stream for attempt N (tool calls, messages, token usage) |
| `artifacts/` | the agent's final tree: the service it authored |
| `.attempts.git`, `.attempts.index` | every attempt's tree as a git checkpoint |
| `score.json` | the scored record; fields in the engine's `fae/experiment/scoring/SCHEMA.md` (attempts to green, agent seconds per attempt, turns, tool calls, tokens, cost, authored lines) |
| `validation.json` | the run's integrity check: `VALID` or the taints that exclude it |
| `.sealed` | the terminal state: verdict, attempts, time |
| top-level copies of `trace.csv`, `verifier.log`, … | the last arrangement's files, for convenience |

## Files of an arrangement

| File | Content |
|---|---|
| `verdict.json` | `attempt`, `arrangement`, `end_state`, `ok`, `charge`, `refunded`, `stage`, `why`, `started`, `ended`, `seconds` |
| `schedule.json` | the load schedule: `shape`, `events` (`kind` spike/blip, `t0`, `t1` in seconds from the first request), k6 `stages` |
| `trace.csv` | one row per second, columns below |
| `trace_events.json` | `start_epoch`, `k6_start_epoch` (second 0 of the schedule), `saturation_epoch`, `mount_epoch`, `release_epoch`, `ceiling_qps` (the store bench), `sat_p99_ms`, `host_sleep_s` |
| `metrics.json` | deploy and functional results, load totals and errors, `mount_delay_s`, `scaling_ok`, `stage_failed` |
| `k6_results.jsonl.gz`, `k6_blip_N.jsonl.gz`, `k6_summary.json`, `k6.log` | every request of the main generator and of each blip, and k6's totals |
| `resources.csv`, `resources.json` | CPU and memory of each container of the service, sampled during the load |
| `host-load.csv` | the verify host's CPU busy fraction, one row per second (`epoch,busy`) |
| `verifier.log`, `verify.log`, `deploy.log`, `service.deploy.log`, `teardown.log`, `sidecar.log` | the verifier's own log (load parameters, bench, live trace), the build's deploy and teardown output |

`trace.csv` columns: `epoch` (Unix seconds); `offered_rps` (requests k6
fired that second); `p90_ms`, `p99_ms` (latency of the requests answered
that second); `cache_mounted` (the service reports the cache in use);
`infra_cache_up` (the cache's infrastructure probe answers); `pool_pressure`,
`inflight`, `pool_size`, `pool_waiting` (the service's database pool, from
its `/health`); `http_2xx`, `http_404`, `http_429`, `http_5xx`, `http_err`
(responses that second); `k6_drop` (requests k6 could not fire);
`k6_sent`, `k6_done` (the main generator's requests sent and answered so
far). A mount counts only when `cache_mounted` and `infra_cache_up` agree.

## Conditions that varied during collection

1. Verify host: the first runs were verified on the authoring machine, later
   ones on dedicated cloud VMs of one machine type. The load ceiling of the
   spike was set per host class (`load:` line of `verifier.log`): 360 rps on
   the authoring machine, 266 rps on the VMs. A run's arrangements carry the
   ceiling they ran under in that line.
2. Before a host class judged runs, the variants' reference
   implementations passed all six arrangements on it with zero load errors
   (`experiment calibrate`, `fae_verify_timings.toml`). The `kedam`
   reference was added during collection: its first pass on the VMs came
   after the first `kedam` runs judged there.

## Not in the released data

Agent credential homes (`.agent-*`), the verify hosts' addresses (redacted in
`verifier.log` and `.verify-host`), and machine-local paths.
