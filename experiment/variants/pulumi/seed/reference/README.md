# Pulumi reference implementation

The committed ANSWER for the pulumi arms: `overlay/app/` holds the authored
files — `cache_policy.py` (in-code, UNGOVERNED provisioning: the app itself
drives Pulumi against the cell's dind sidecar to run/destroy the Redis
container on demand), `cache.py`, and the wired `health.py` / `main.py` /
`routes/photos.py` (read-through + truthful `cache_mounted`).

Provenance: promoted verbatim from the first green pulumi cell
(`opus_high_pulumi_handed_T1_r1`, run-2) — a verified solution, not a
hand-written ideal. Replace deliberately if a cleaner canonical answer is
ever authored.

The app skeleton and bring-up are the rig's own (`task/skeleton`, the
variant files' `[verify.run]`; the dind sidecar comes from the `Pulumi`
infra class, `experiment/variants/pulumi/__init__.py`), so a reference run
is bit-identical to a cell verify.

## Run

```bash
python3 cli.py experiment smoke --variants pulumi_sealed_apidocs   # this reference cell through the driver
# or by hand, outside the scored tree (AGENT=ref WORKSPACES_DIR=$PWD/ws-smoke.nosync in the environment):
REFERENCE=1 python3 -m fae.cell.prepare T1 pulumi_sealed_apidocs 1
REFERENCE=1 python3 -m fae.cell T1 pulumi_sealed_apidocs 1 --stub "$(mktemp -d)"
```
