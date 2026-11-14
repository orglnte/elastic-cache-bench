"""The verify host probe: the capped store's throughput on one host, benched
as the verifier benches it (rig.db_bench_qps), on a store of the probe's own.
It runs rig.py in a child whose DOCKER_HOST names the host, so every docker
call of the bench goes to that host's daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from fae.cell.verify_host import VerifyHostProbe

RIG = Path(__file__).with_name("rig.py")
STORE_KEYS = ("DB_IMAGE", "DB_NAME", "DB_CPUS", "DB_MEM", "DB_MAX_CONN", "DB_WARM_SECS",
              "DB_BENCH_CLIENTS", "DB_BENCH_SECS", "QUERY_CPU_OPS")
TIMEOUT_S = 600


class StoreBenchProbe(VerifyHostProbe):
    def measure(self, host, conf, log=print):
        store = {k: str(conf.get(k)) for k in STORE_KEYS if conf.get(k)}
        env = host.env()
        if host.remote:
            env.pop("CPUSET_MEASURED", None)    # a remote host's cores are all the verify's
        tag = f"probe-{os.getpid()}-{int(time.time())}"
        log(f"probe: store bench on {host.docker_host or 'the local daemon'} ({tag})")
        r = subprocess.run([sys.executable, str(RIG), "store-bench", tag, json.dumps(store)],
                           env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
        if r.returncode:
            raise RuntimeError(f"store bench did not run on {host.docker_host or 'the local daemon'}: "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")
        return {"store_qps": float(json.loads(r.stdout.strip().splitlines()[-1])["store_qps"])}
