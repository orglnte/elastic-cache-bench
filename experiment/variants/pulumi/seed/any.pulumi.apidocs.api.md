# Pulumi Automation API reference — in-code dynamic provisioning

Reference for the runtime-provisioning surface: the **Pulumi Automation API**,
driven from the service's own code, provisioning against the project's Docker
endpoint.

## Model

Provisioning is **imperative, in-code, and self-managed**. The app and the
transactional store are **given** — the app runs as a local host process and
its `DB_DSN` is injected; you do not provision either. Your code provisions
the **Redis cache** at runtime by executing a Pulumi program in-process, and
releases it the same way. There is no external controller, no reconciler, and
no governing daemon: the code that decides is the code that effects, and
everything an external system would normally track — what exists right now,
whether a create is a duplicate, whether a destroy left orphans, what happens
to in-flight requests during a transition — is the author's responsibility.

## Runtime environment (given)

The service runtime provides, as environment variables:

- `PULUMI_DOCKER_HOST` — the Docker endpoint your program must target (set it
  as the `docker` provider's `host`). This is the project's provisioning
  substrate; resources you create live there.
- `PULUMI_CACHE_HOST` / `PULUMI_CACHE_PORT` — where the app must reach the
  cache once it exists. The substrate maps the cache's published port `6379`
  to this address; publish the container's `6379` and the mapping holds.
- `PULUMI_BACKEND_URL` + `PULUMI_CONFIG_PASSPHRASE` — a local, file-based
  Pulumi state backend, pre-configured; no cloud account is involved.

The `pulumi` engine and the `pulumi` / `pulumi_docker` Python packages are
pre-installed in the service runtime.

## Automation API — the surface

The Automation API runs Pulumi as a library: define the desired resources as
an **inline program** (a Python function creating resources), bind it to a
**stack** (named state), and drive it imperatively:

```python
from pulumi import automation as auto

def cache_program():
    import pulumi_docker as docker
    provider = docker.Provider("dind", host=os.environ["PULUMI_DOCKER_HOST"])
    docker.Container("cache",
        image="redis:7-alpine",
        ports=[docker.ContainerPortArgs(internal=6379, external=6379)],
        opts=pulumi.ResourceOptions(provider=provider))

stack = auto.create_or_select_stack(
    stack_name="cache", project_name="svc", program=cache_program)
stack.up()        # provision (blocks until the resource exists)
stack.destroy()   # release (blocks until it is gone)
```

- `create_or_select_stack` — idempotent handle to the named state; safe to
  call repeatedly.
- `stack.up()` — reconciles the stack to the program's declared resources.
  Calling `up()` on an already-provisioned stack is a no-op refresh, not a
  duplicate create — *provided the same stack name is used*. Two different
  stack names declaring the same container name will conflict at the daemon.
- `stack.destroy()` — tears down everything the stack tracks. Resources
  created outside the stack's state (a partial failure, a renamed stack) are
  **orphans**: nothing reclaims them.
- Failures surface as exceptions at the call site. A failed `up()` may leave
  the stack partially provisioned; the state backend remembers what was
  created, and the next `up()`/`destroy()` proceeds from that record.

## Operational notes

- `up()`/`destroy()` are **slow relative to a function call** (an engine run
  each; seconds, not milliseconds). Do not put them on the request path;
  drive them from a background control task and let requests read the
  current state.
- The engine serializes operations per stack; concurrent `up()` calls on one
  stack fail with a lock error. Your code decides how conflicting intents
  (two workers both deciding to mount) converge.
- After `destroy()`, connections to the released resource fail immediately;
  the app must already be routing around it. Nothing external drains or
  fences traffic for you.

## How to: drive up/destroy from a noisy signal

`up()`/`destroy()` provision and destroy real infrastructure — they are not
cheap toggles. Calling them from the instantaneous value of a fluctuating
signal churns the resource (mount/unmount cycles that serve nothing).
Standard control-loop practice applies:

1. **Act on a condition that holds, not on a single sample.** Require the
   signal to stay past the threshold for several consecutive samples before
   acting, in BOTH directions.
2. **Make the directions asymmetric (hysteresis).** If the release condition
   is the mirror of the acquire condition, the resource oscillates at the
   boundary.
3. **Expect the signal to move BECAUSE of your action.** Once a cache absorbs
   the hot reads, the pressure that justified it drops — a naive
   destroy-on-low reading tears down the resource that is doing its job.
   Judge release on the demand the resource is currently serving, not only on
   the signal that triggered the mount.

## Validate offline before submitting

The provisioning is ordinary Python, so the decision logic is **unit-testable
with no daemon or store**: isolate the policy from the Pulumi calls and assert
it directly — sustained saturation mounts the cache, a short blip does not,
and drain releases it. `python3` and `pytest` are available. Testing the
behavior this way is far cheaper than a deploy cycle and catches control-logic
mistakes (e.g. releasing on the same signal that triggered the mount)
instantly.
