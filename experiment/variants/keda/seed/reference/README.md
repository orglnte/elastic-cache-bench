# KEDA reference implementation

The committed ANSWER for the keda arms (keda_sealed and keda_access verify
identically): `overlay/` holds exactly the files an agent would author —
`app/provisioning_impl.py` + `k8s/{redis,scaledobject,prometheus,json-exporter}.yaml`
(cache scaled 0↔1 by an external KEDA autoscaler reacting to the exported
`pool_pressure` metric — not in-code). The app skeleton and the bring-up are
the harness's own (`seeds/skeleton`, `harness/bringup/keda`), so a reference
run is bit-identical to a cell verify.

Infra (kind + KEDA + `ecb` namespace): `python3 cli.py experiment infra`
— idempotent, left up between runs.

## Run

```bash
python3 cli.py experiment smoke --variants kedap_sealed_apidocs   # this reference cell through the driver
# or by hand, outside the scored tree (AGENT=ref WORKSPACES_DIR=$PWD/ws-smoke.nosync in the environment):
REFERENCE=1 python3 -m fae.cell.prepare T1 kedap_sealed_apidocs 1
REFERENCE=1 python3 -m fae.cell T1 kedap_sealed_apidocs 1 --stub "$(mktemp -d)"
```

Every reference faces the SAME fixed store and the SAME spike; mount is
infra-gated for all of them.
