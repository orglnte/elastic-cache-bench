# Workspace — photoshare service

A small FastAPI service backed by Postgres. As seeded, it is complete and
running: all endpoints serve, every read goes to Postgres, `/metrics` exports
the load signals, `/health` reports liveness and pool state.

Layout:

- `app/` — the service. `main.py` wires the lifespan (pool from `$DB_DSN`,
  migration, load-signal loop); `routes/` per-task endpoints; `admission.py`
  bounded admission gate; `load_signal.py` pool-pressure sampling; `db.py`
  pool + schema + the ranked query; `health.py` `/health` + `/metrics`;
  `settings.py` env-read process settings.

The bring-up, the teardown and the load profiles are the platform's and run
outside this repo. Your task is described in the prompt you received.
