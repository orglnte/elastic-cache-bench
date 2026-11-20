# AGENTS.md — elastic-cache-bench

This repo is one FAE experiment: coding agents author the
runtime-provisioning slice of a fixed photo-sharing service with KEDA
(`prometheus` scaler), KEDA-hint (`metrics-api` scaler) or Pulumi, each
sealed and with access. The engine is the sibling checkout `../fae` (or
`$FAE_DIR`), imported through `cli.py`, never installed. The engine's
invariants — cells, the ledger, locks, the verifier contract, verdicts, data
destruction — are [`../fae/AGENTS.md`](../fae/AGENTS.md). This file carries
what belongs to this experiment, each rule with the failure it prevents.

## Set up and run

1. **Engine:** check out fae beside this repo (`../fae`), or set `FAE_DIR`.
2. **Config:** `python3 cli.py experiment init` writes `fae.toml`
   (machine-local, gitignored). Set the paths, the agents' credentials homes
   (`python3 cli.py experiment credentials AGENT`) and the verify hosts
   (`VERIFY_DOCKER_HOST`, one or more `ssh://user@host` daemons).
3. **Tune the load once** on one verify host with "Host tuning" below, and
   set `[load]` in `fae.toml` until it passes.
4. **Calibrate every verify host**, idle, one at a time:
   `python3 cli.py experiment calibrate --host ssh://user@host`. It measures
   the store, runs every variant's reference on all six arrangements and the
   load-shape guard, and writes the host's section of
   `fae_verify_timings.toml` (machine-local, gitignored). `--repeats 3`
   measures the spread instead of estimating it.
5. **Check:** `python3 cli.py experiment check` must print READY; it fails
   on a host without a section.
6. **Queue and run:** `python3 cli.py queue add AGENT --matrix --to-rep N`,
   then `python3 cli.py experiment run`.
7. **Watch:** `python3 cli.py experiment status`; the run log names every
   finished cell, alert and repair.
8. **Add a verify host later:** calibrate it (step 4) before it joins
   `VERIFY_DOCKER_HOST`; a verify on an uncalibrated host stands its cell
   down with the command.

## Rules that bite here

1. **The fingerprint covers `experiment/**` and the engine's `fae/cell/**`
   and `fae/experiment/**`.** An edit to any of them while a cell runs
   voids that cell's next verify. Edit them only with no cell and no smoke
   running.
2. **`fae.toml` is machine-local and gitignored**: this host's sibling
   paths, each agent's credentials home, the concurrency caps, the load
   tuning. The defaults it is rendered from are in git
   (`experiment/__init__.py`, `CONFIG`), and so are the agents compared
   (`experiment/agents.toml`). Never put a secret in it.
3. **Workspaces and the lock plane live under this root**
   (`workspaces.nosync/`, `ws-smoke.nosync/`, and the scheduling plane
   `workspaces.nosync/.queues`, `.conduct`, `.locks`, `transitions.log`);
   `ws-archive.nosync/` keeps cells as evidence. Two-stage delete only
   (`.to_be_deleted/<ts>/`), never `rm -rf`.
4. **Each arm's agents see only their own arm's tools.** The agents' base
   (`Dockerfile.agent-base`, at this root) carries every model's client and
   nothing an arm authors with; each variant's `agent/Dockerfile` is its
   family's layer over it (keda: kubeconform, kubectl; pulumi: the docker
   CLI; every arm: k6, pytest, psycopg, redis). The skeleton every arm
   receives names no arm: a tool or a mention shared across arms hands one
   arm's agent another's interface.
5. **A wiped workspace retires its cell** (`Retire` in the transitions log):
   the id then names a new cell. Redo a cell by a fresh requeue, never by
   editing its ledger.
6. **Every verify host is calibrated before the experiment runs on it.**
   The load is fixed numbers (rps, seconds) and the release limits are
   fixed seconds of the schedule, tuned to one host's store. A host with a
   weaker or stronger store puts the knee elsewhere, and the same build can
   pass on one host and fail on another. Each host's section of
   `fae_verify_timings.toml` records the load it was calibrated with and
   what the references measured on it, and its verifies run with that load;
   recalibrate a host when it or the experiment changes. Lower `top` rather
   than lengthen `ramp_s` to move saturation later: the schedule's seconds,
   and so the release windows, stay the same.

## The variants

One file per variant, `experiment/variants/<arm>_<docs>.toml`, its id the
cell id's slot: `pulumi_sealed` and `pulumi_access`, and the KEDA families,
KEDA (`kedap_*`) and KEDA-hint (`kedam_*`), sealed and access — six, all
`apidocs`. Each file names everything its agent reads
(`[authoring.inputs]`: the prompt, the project layout, the api doc), its
`factors` (`tech`, `access`, `docs`) and its declarative `[substrate]` (`docker`
or `k8s`, `lifecycle = "split"`, `access = false|true`) with a thin
`DomainOverlay` per tech (`KedaOverlay`, `PulumiOverlay`).
There is one task, T1 (photoshare).

**KEDA and KEDA-hint.** One substrate, one overlay, one image, one contract;
they differ in the scaler the docs allow (each file's api doc input, and its
`[substrate] params.scaler`). KEDA (`kedap_*`; `any.kedap.apidocs.api.md`) is
bound to the `prometheus` scaler, the policy in PromQL recording rules;
KEDA-hint (`kedam_*`; `any.kedam.apidocs.api.md`) to `metrics-api`, the
policy computed in the app and exported as a field. The verify refuses a
build whose ScaledObjects use another trigger type (`scaler_violation`, a
charged `Rejected` right after the apply). The two families read different
docs and are not pooled.

**The secure runner.** The judged service runs in the cell's secure runner
(`ctx.runner` → `fae-secrun-<cid>`): one core of its own, a copy
of the artifacts, the cell's network, no repo, no workspace and no Docker
socket; the verify container keeps one core for k6, the trace and the rig.

## Infra per tech

Everything a cell provisions sits on its network `fae-net-<cid>` and is
reached by name: the store `fae-store-<cid>:5432` (started via
`ctx.start_fixture`), a verify sidecar's daemon `fae-vdind-<cid>:2375`
(`ctx.substrate.docker_host`) and the cache it publishes, the judged service
`fae-secrun-<cid>:8080`.

In every variant the verifier runs the service, the command of the
variant's `[verify.run]`, through `ctx.runner`
(`experiment/variants/service.py`), from the
verify image, over a fresh copy of the artifacts (`<ws>/.verify-out/run`),
with a scratch dir for its own state (`<ws>/.verify-out/secrun-scratch`:
HOME, the pulumi variants' state backend). Per arrangement it clears a
stale runner, makes the copy, calls `ctx.infra.verify_setup` (which
provisions `ctx.substrate` + the tech's `DomainOverlay` and returns the
service's environment and networks), starts the service and
holds `/health`; on teardown it stops the service (keeping
`service.deploy.log`) before `ctx.infra.verify_teardown` (and FAE tears down
labeled fixtures and `ctx.substrate` in `finally`). What a
bring-up rewrites (keda's `APP_HOST`) is rewritten in that copy, never in
the judged tree.

- **`kedap_*` / `kedam_*`** — `[substrate] kind = "k8s", lifecycle = "split"`
  with `cluster_prefix = "ecb-ka-"` and `KedaOverlay`. Every verify provisions
  its own throwaway kind cluster (KEDA, metrics-server and the namespace
  installed) and deletes it at teardown. The verify container joins the
  cluster's network and uses the internal kubeconfig; the service's runner
  joins it too, so the cluster's scraper reaches it: every `APP_HOST` in the
  service's copy of the manifests is rewritten to the runner's name, and that
  copy is what is applied. The cache's port-forward runs in the verify
  container and listens where the runner reaches it (`CACHE_HOST` = the verify
  container). `KedaOverlay` waits for metrics-server's rollout even on a
  cluster found already there, so a half-settled cluster is never reused
  unchecked. The access variants (`access = true`) also get a per-cell sandbox
  cluster for authoring and take the `keda_access` lock (one pool for every
  keda access variant, both scalers); the verify never adopts that kubeconfig,
  so the sandbox is neither graded nor touched.
- **`pulumi_sealed` / `pulumi_access`** — `[substrate] kind = "docker",
  lifecycle = "split"` with `PulumiOverlay`. The agent's Pulumi program runs
  inside the service, on the service's core, with its state backend in the
  runner's scratch, talking to a fresh per-verify DinD (`fae-vdind-<cid>`).
  `pulumi_access` (`access = true`) gets a separate cell-lifetime DinD sandbox
  for authoring; `PulumiOverlay.on_verify_teardown` resets inner state before
  FAE removes the verify DinD.

## The gate and the load law

Six arrangements, `BBS/BSB/SBB/SSB/SBS/BSS` (blip, spike), each a full
deploy → load → teardown cycle; all must pass (`shapes=all`). The law is the
engine's contrib `elastic_resource` block; its parameters are `[load]` in
`fae.toml`:

| key | default | meaning |
|---|---|---|
| `baseline` | 18 | rps between events, below any knee |
| `top` | 360 | the ramp's ceiling, well above this host's knee |
| `ramp_s` | 20 | the spike ramps baseline → top over this; a blip rides the same slope |
| `hold_s` | (empty) | hold at the top after the ramp: the block's default, 30 s (25 s in multi-spike arrangements) |
| `blip_p99_ms` | 50 | a blip's trigger: the first request slower than this, or a request queued for the pool |
| `blip_excess_req` | 10 | requests a blip offers past its trigger rate before it aborts |
| `sat_p99_ms` | 150 | saturation: the first spike tick with p99 at or above this |
| `max_mount_s` | 30 | the cache must be up within this of the saturation |

`blip_excess_req` is the one blip knob that is a size, and it is bounded
from above by the references' own sustain: the queue a blip leaves must
clear inside one scrape → rule → poll cycle of the keda reference (1 s each,
a 5 s sustain window), about 2–3 s. At 10 the queue clears in ≤ 3 s and no
reference mounts on a blip; at 30 it took 4–6 s, the size of that window,
and a reference could mount on it.

**Host tuning** (the method is the engine's, AGENTS.md §7):

1. `experiment smoke --variants pulumi_sealed_apidocs`: `verify.log` prints
   the load line and the store bench (pgbench on the capped store; floor
   `calibration_min_qps` 40). The bench is not the app's knee: the hot read
   is cheaper.
2. Saturation lands 6–8 s into the ramp; the cache mounts about 5–10 s
   after it (pulumi faster, keda slower). Saturation at the top of the ramp
   or `FAIL[load-shape]` means raise `top`; saturation in the first second
   means lower `top` or raise `ramp_s`.
3. Every blip stops by its rule, `pool_waiting` back to 0 within 3 s, and no
   `cache_mounted` during it. A long queue means lower `blip_excess_req` or
   raise `ramp_s`.
4. `experiment smoke --full-gate`: every variant × six arrangements, all
   green, zero load errors.
5. `LOAD_TOP_RPS=60 python3 cli.py experiment smoke --variants pulumi_sealed_apidocs`
   ends in `FAIL[load-shape]`, refunded, with the ledger's
   `ALERT LOAD-SHAPE`.

**The store bench guards the host.** A verify whose bench falls under
`calibration_min_qps` sleeps and re-benches instead of judging (up to
`MAX_REBENCHES`, 3, because it holds the verify lock) and writes an
`ALERT HOST-OVERLOADED` line; a bench that produced no number at all is
`FAIL[store]` at once. An arrangement during which the host slept is the
engine's to void and run again (stage `host-sleep`, fae AGENTS.md §7); the
trace keeps `host_sleep_s` as evidence.

## Contracts: the promises the docs make

After every bring-up and every teardown the arm's contract
(`experiment/variants/<tech>/verify/contract.py`) checks the world against
the seed docs: every `APP_HOST` rewritten, Service `redis` resolving to a
Deployment, KEDA and metrics-server present, the verify cluster gone after
teardown (keda); Pulumi state empty and the port owned by the service the
bring-up started (pulumi).

## Taint rules

`experiment/taint.py`, run by `results validate`:

1. **A cell that never mounted is a rig suspicion, not a verdict**: an
   unbroken run of `saw 0` across two or more scaling attempts taints only
   when no cell of that arm has ever mounted — otherwise siblings prove the
   path works and it is an authoring failure (a warning). Greens are exempt.
2. **Load errors taint only when the mount episodes matched in count and
   timing** (`mounts_matched`, parsed from `scaling_why`); otherwise the
   errors are the measured failure itself. The parser is pinned by selftest
   cases taken verbatim from real `metrics.json`.
3. **k6 unavailable taints only an e2e-green build**, since k6 is consulted
   only after e2e passes.
4. **Junction rules 12–16** match rig defects by their own signature in the
   rig-written logs: a charged store failure; `APP_HOST` never rewritten
   (the bring-up's note, or the scraper's own `prom-targets.json` still
   targeting `APP_HOST`); a cache Deployment the mount probe could not see; a
   ClusterIP collision; a ghost container or stack lock. A green cell with a
   junction defect keeps its verdict and is warned: its attempt count is
   inflated.
