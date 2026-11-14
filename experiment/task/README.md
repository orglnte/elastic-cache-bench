# task/ — the shared task

Every variant gives the agent the same task:

1. **`T1.PROMPT.md`** — the functional spec and acceptance criteria, the
   same text for every variant. Each variant's file
   (`experiment/variants/*.toml`, `[authoring.inputs]`) places it in the
   agent's workspace as `TODO.md`, beside the variant's own docs (the
   project layout and the framework's API docs).
2. **`skeleton/`** — a fixed FastAPI + Postgres app (routes, store access,
   read-through cache, load-metric export, `/health`, `Dockerfile`). It is
   identical for every variant and depends only on the provisioning seam in
   `app/contracts.py`. The variant's `seed/overlay/` adds the one slice the
   agent authors and the framework's scaffolding.

`fae/cell/prepare.py` assembles the workspace: the skeleton, the variant's
overlay and inputs, and a `.skeleton_manifest` (path and sha of every seeded
file) in the workspace root where the agent cannot write, so scoring can
separate the agent-authored surface from the fixed skeleton.

## Scope

The task is the runtime-provisioning slice only: a Postgres store present
from the start, and a cache scaled 0↔1 at runtime under load. The cluster,
namespace and network are given; the agent declares the cache and the policy
that mounts and releases it.
