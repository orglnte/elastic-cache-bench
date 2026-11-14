# elastic-cache-bench — the experiment definition

A matched-task experiment: coding agents author the **runtime-provisioning
slice** of a fixed web service under three provisioning frameworks, and we
measure how reliably each framework is authored. This directory is the experiment
definition the fae engine runs, imported from the sibling `../fae` through
this repo's `cli.py` (the engine's `AGENTS.md` §0 has the boundary); the
operator runbook is the engine's `README.md`, this repo's rules are its
`AGENTS.md`.

**Arms** are `framework × access`:

1. **`kedap_sealed` / `kedap_access`** — Kubernetes manifests + a KEDA
   `ScaledObject` with the `prometheus` scaler scaling a cache 0↔1; the
   policy is a PromQL recording rule over metrics the app exports.
2. **`kedam_sealed` / `kedam_access`** — the same, with the `metrics-api`
   scaler; the policy is computed in the app and exported as a field.
3. **`pulumi_sealed` / `pulumi_access`** — infrastructure as code: a Pulumi
   program run by the service provisions the same resources.

*sealed* = the agent authors blind; *access* = the live infra (cluster,
docker daemon) is observable while authoring. Every variant gives the agent
the framework's curated API docs (`apidocs`), matched in weight across
frameworks.

The variants are `experiment/variants/*.toml`, one file each: what the agent
is given (template, inputs, surface), how it is judged, and its infra.

## Experiment design, in brief

**Task** (identical for every arm): a working FastAPI+Postgres app is given;
add a dynamically provisioned read-through cache that **mounts under
sustained saturation, does not mount on brief blips, releases when load
drains, never flaps, and serves every request** — verified end-to-end under a
k6 load test by the same treatment-agnostic verifier
(`fae/cell/verify.py`). Agents author only the provisioning slice on
a fixed skeleton (`task/skeleton/` plus the variant's `seed/overlay/`).

**Measure:** iterations-to-green (ITG) over an attempt budget (10 per cell in
the current campaigns), green rate, authored surface (SLoC).

**Agents and models.** One cell = `(model, variant, task, rep)`.
`experiment/agents.toml` declares the agents (`claude`, `agy`, `opencode`:
one CLI and one credential each) and the models they run, each tag with its
agent and model id: Claude models via `claude`, Gemini via `agy`, OSS models
(deepseek, kimi) via `opencode`. Each agent's credentials home is
machine-local (`fae.toml`), set up with `cli.py experiment credentials AGENT`.

## Layout

```
__init__.py          the definition the engine reads: variants, matrix, seed docs,
                     gate, config keys, verifier, exclusive
                     lock, verbs, taint rules, reference workspace, pooled models
task/                the shared task: skeleton/ (every variant's common files)
                     and the prompt (T1.PROMPT.md)
variants/            one directory per tech (keda/ pulumi/), each: the two
                     arm classes (__init__.py, sealed and access) with their
                     verify_setup/verify_teardown pair — the world one
                     arrangement runs on, brought up inside the verify
                     container — seed/ — the agent's side: overlay/ over the
                     skeleton, the doc packs (T*.<tech|arm>.project_layout.md,
                     any.<tech>[.<condition>].api.md) and reference/overlay/ (the
                     stub answer) — and verify/ — the variant's image layer
                     (Dockerfile, FROM the verifier's) and contract.py;
                     service.py is what the pairs share (the service as a process of the verify
                     container, the port check, Rejected); each variant's
                     agent/ is its agents' layer over ../Dockerfile.agent-base,
                     its own tools only (keda: kubeconform, kubectl; pulumi:
                     the docker CLI; every arm: k6,
                     pytest, psycopg, redis)
verifier/            the shared verify side: ElasticCacheVerifier (__init__.py), its image
                     (Dockerfile: python, docker CLI, k6, the service's runtime,
                     pinned), the k6 load profiles (loadgen/), the store and cache
                     guards (rig.py), the contract base (contract.py)
taint.py             what a rig fault looks like in this experiment's evidence
exp1.py              the reference-mechanism benchmark (one arrangement per arm)
scoring/             the scoring rubric, the reference implementations, the
                     trace-to-PDF figure tool
tests/               this experiment's tests (`python3 -m unittest discover -s experiment/tests`)
```

Everything under this directory is in the verify fingerprint: an edit here
between a cell's start and one of its verifies voids that attempt. Edit in a
drain window.
