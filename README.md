# elastic-cache-bench

A benchmark for coding agents: each agent authors the runtime-provisioning
slice of a fixed web service, an elastic cache that must mount under
sustained load and release when it drains, with one of several provisioning
frameworks (KEDA with the `prometheus` scaler, KEDA with `metrics-api`,
Pulumi). Every build is judged by the same load test and the same law. The
experiment runs on the [fae](../fae) engine; its definition, task, variants,
verifier, grading rules and tests live under `experiment/`
([`experiment/README.md`](experiment/README.md)).

## The engine is imported, not installed

`cli.py` runs the engine from its checkout: `$FAE_DIR`, else the sibling
`../fae`. It puts that checkout on `PYTHONPATH`, so every process the engine
starts (cells, verifiers, grading scripts) imports the same engine, and a fix
to the engine is an edit there, nothing to reinstall. The root is this repo,
whatever the working directory.

## Run

The step-by-step setup is in [`AGENTS.md`](AGENTS.md) ("Set up and run").
In short:

```sh
python3 cli.py experiment init        # first time: writes fae.toml (machine-local, gitignored)
python3 cli.py experiment check       # definition, variants, invariants, infra, agents' images, leftovers
python3 cli.py experiment smoke       # the references, one arrangement (--full-gate: all six)
python3 cli.py queue add AGENT --matrix --to-rep N
python3 cli.py experiment run
```

The engine's runbook is its [`README.md`](../fae/README.md); the invariants
are [`AGENTS.md`](AGENTS.md) here and the engine's.

## Tests

```sh
DOCKER_HOST=tcp://127.0.0.1:1 python3 -m unittest discover -s experiment/tests
```

They load the engine's test context from the same checkout `cli.py` uses.
