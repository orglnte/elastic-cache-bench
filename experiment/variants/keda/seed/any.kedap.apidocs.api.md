---

# Conventional stack reference — local Kubernetes + KEDA

Reference for the conventional runtime-provisioning surface: declarative
Kubernetes manifests for the
Redis cache + a metric pipeline + an external autoscaler that scales the cache
0↔1 on the app's exported signals.

## Model

Provisioning is **declarative, external, and reactive**. The app and the
transactional store are **given** — the app runs as a local host process, and its
`DB_DSN` is injected; you do not declare either. You declare the **Redis cache**
and an **autoscaler** that reacts to the app's load signals asynchronously and
scales the cache Deployment **0↔1**; the app reads `CACHE_HOST`/`CACHE_PORT` and
**discovers** whether the cache is reachable right now. The app never provisions.

Substrate (assumed to exist, out of scope): a local **kind/minikube** cluster,
**KEDA**, metrics-server, and the target namespace.

The app **exports its load signals** at `GET /metrics` as **JSON** (pool
pressure, p99 latency, rps). The app runs on the **host**, so anything in the
cluster that reads `/metrics` must reach it at the host address (write the
target as `http://APP_HOST:8080/metrics`; the bring-up rewrites `APP_HOST`).

## Redis cache — the Deployment the autoscaler scales

Declare the cache in `k8s/` as a Deployment + Service (Kubernetes `apps/v1`
Deployment, `v1` Service — standard resources). The Service name and ports are
fixed by the project's naming contract — see `docs/project.md`; the bring-up
port-forwards that Service to the app's `CACHE_HOST:CACHE_PORT`. The replica
count belongs to the autoscaler, not to a human: decide what the Deployment's
declared `replicas` must be so the cache is not running while idle, and let the
autoscaler own the 0↔1 transitions. A `redis:7-alpine` image is pre-loaded in
the cluster.

## Metric pipeline — expose the app's signals to the autoscaler

This project scales with KEDA's **`prometheus` scaler, and only with it**: every
trigger of every `ScaledObject` must be `type: prometheus` — the verification
rejects a build whose triggers use any other scaler.

The autoscaler runs outside the app and cannot read in-process state, so you
must scrape and (as the policy requires) transform `/metrics`. A **Prometheus**
Deployment scraping the app is the general tool; where the policy needs a value
Prometheus computes (a conjunction, a rate, a sustained window), express it as a
**recording rule** and have the trigger read the recorded series. The app's
`/metrics` is JSON, not Prometheus text exposition — if your scraper needs the
text format, a small exporter/sidecar bridges it.

Match the scrape/eval cadence to how fast the policy must react; a slow interval
manufactures lag. Keep the recorded metric names stable — the trigger reads them
by name.

## Autoscaler — scale the cache 0↔1

A **KEDA `ScaledObject`** targeting the Redis Deployment. The fields that
express a scaling policy (see the KEDA docs for full semantics):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
spec:
  scaleTargetRef: { name: <your cache Deployment> }
  minReplicaCount: <int>       # replica floor when every trigger is inactive
  maxReplicaCount: <int>       # replica ceiling
  pollingInterval: <seconds>   # how often KEDA evaluates the triggers
  cooldownPeriod: <seconds>    # inactive time before scaling back to the floor
  triggers:
    - type: prometheus
      metadata:
        serverAddress: <your prometheus Service URL>
        query: <the series/expression your rule records>
        threshold: <value>            # desired replicas = ceil(value/threshold)
        activationThreshold: <value>  # below this the trigger is INACTIVE (0-case)
```

Every value is yours to choose from the policy you are implementing.

Semantics that matter for a 0↔1 policy: multi-trigger scale-UP is **OR**
(desired = max over triggers); scale-to-zero happens only when EVERY trigger is
inactive; and a scale-**from-zero** (0→1) is not debounced by the usual HPA
stabilization window, so any conjunction or sustain the policy demands must
live in the metric value itself (the recording rule), not only in the trigger.
Express both the acquire and the release conditions faithfully — a plain
single-metric trigger will not capture a conjunction or a sustain on its own.

## How to: drive scale-up/down from a noisy signal

A `ScaledObject` scales real infrastructure (the cache Deployment) 0↔1 — not a
cheap toggle. A trigger keyed on the instantaneous value of a fluctuating
signal churns it. Standard control-loop practice applies:

1. **Act on a condition that holds, not on a single sample.** Require the
   signal to stay past the threshold across several evaluations before scaling,
   in BOTH directions.
2. **Make scale-up and scale-down asymmetric (hysteresis).** If scale-down is
   the mirror of scale-up, the replica oscillates at the boundary.
3. **Expect the signal to move BECAUSE of your action.** Once the cache scales
   up and absorbs the hot reads, the pressure that justified it drops — a naive
   scale-down-on-low reading tears down the replica that is doing its job.
   Judge scale-down on the demand the cache is currently serving, not only on
   the signal that triggered the scale-up.

## Application code (discovery only)

The app-side change is **discovery, not control**: route hot reads through the
cache when it is reachable at `CACHE_HOST:CACHE_PORT`, fall back to the store
when it is not. No provisioning or scaling calls from app code.

**Reachability means a round-trip, not construction**: building a Redis client
object is lazy and always "succeeds" — it proves nothing. Gate on an actual
`ping()` (short timeout) before reporting a client. The app's `/health`
`cache_mounted` field mirrors this — it must be true only while a cache backend
actually exists and answers.

## Consistency surface (where drift happens)

The artifacts that must agree: the Redis Service name ↔ the autoscaler
`scaleTargetRef` ↔ the scrape target ↔ the recorded metric name the trigger reads
↔ the metric key the app exports ↔ the app's `CACHE_HOST` read. A name/key that
drifts in any one of these is a cross-artifact consistency defect.

## Reference source

Standard, well-documented tooling: the Kubernetes Deployment/Service API, the
Prometheus scrape/recording-rule config and the KEDA `ScaledObject` + `prometheus` scaler spec. Use idiomatic,
production-grade configs.

## Validate offline before submitting

The manifests are declarative YAML: validate their **structure** offline with
no cluster using `kubeconform <file.yaml>`, which checks them against the
Kubernetes and KEDA schemas. This catches malformed specs and schema errors
before a deploy cycle. (The scaling *behavior* — thresholds, sustain, release
— can only be observed under load by the verification, not asserted offline.)
