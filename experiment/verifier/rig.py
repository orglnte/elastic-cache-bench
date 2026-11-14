"""The experiment's rig: the capped store, the cache-backend probes and
guards, the load-shape derivation.

Every function takes the env the verify hands it and shells out through
`_run`, from inside the verify container over the daemon's socket;
nothing here takes a lock.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time


def _run(argv, input_text=None, timeout=None, env=None):
    p = subprocess.run(argv, input=input_text, capture_output=True,
                       text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def mem_bytes(v):
    s = str(v)
    unit = s[-1:]
    if unit in "gG":
        return int(s[:-1]) * 1024 ** 3
    if unit in "mM":
        return int(s[:-1]) * 1024 ** 2
    if unit in "kK":
        return int(s[:-1]) * 1024
    return int(s)


# --- cache-backend hygiene ---------------------------------------------------

def _redis_ping(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=2) as s:
            s.sendall(b"PING\r\n")
            s.settimeout(2)
            return b"PONG" in s.recv(64)
    except OSError:
        return False


def _cache_containers():
    rc, out, _ = _run(["docker", "ps", "--format", "{{.Names}}"])
    if rc != 0:
        return []
    names = []
    for n in out.split():
        low = n.lower()
        for i, ch in enumerate(low):
            if low.startswith(("redis", "cache"), i) and \
                    (i == 0 or low[i - 1] in "-_"):
                names.append(n)
                break
    return names


def _is_cache_name(name):
    low = name.lower()
    return any(low.startswith(("redis", "cache"), i) and (i == 0 or low[i - 1] in "-_")
               for i in range(len(low)))


def _k8s_cache_deployments(ns, env=None):
    """The Deployments that are the cache backend: any named redis/cache,
    plus whatever serves behind Service `redis` — the docs fix the Service
    name, the Deployment's is the agent's to choose — matched by the
    Service's selector against each Deployment's pod-template labels.
    Nothing where the image carries no kubectl (a variant with no cluster)."""
    if not shutil.which("kubectl"):
        return []
    rc, out, _ = _run(["kubectl", "get", "deploy", "-n", ns, "-o", "json"], env=env)
    if rc != 0:
        return []
    try:
        items = json.loads(out).get("items") or []
    except ValueError:
        return []
    selector = {}
    rc, sout, _ = _run(["kubectl", "get", "svc", "redis", "-n", ns, "-o", "json"], env=env)
    if rc == 0:
        try:
            selector = json.loads(sout).get("spec", {}).get("selector") or {}
        except ValueError:
            selector = {}
    names = []
    for it in items:
        name = it.get("metadata", {}).get("name") or ""
        labels = (it.get("spec", {}).get("template", {}).get("metadata", {})
                  .get("labels") or {})
        behind_svc = bool(selector) and all(labels.get(k) == v for k, v in selector.items())
        if name and (_is_cache_name(name) or behind_svc):
            names.append(name)
    return names


def assert_no_cache_backend(env):
    """None when clean, else the leftover's description (the guard both at
    run start — prior teardown failed — and at run end)."""
    ns = env.get("NAMESPACE") or "ecb"
    ph, pp = env.get("PULUMI_CACHE_HOST"), env.get("PULUMI_CACHE_PORT")
    if ph and pp and _redis_ping(ph, pp):
        return f"live redis behind the pulumi sidecar mapping ({ph}:{pp})"
    deploys = _k8s_cache_deployments(ns, env)
    if deploys:
        return f"k8s cache Deployment in ns/{ns} ({' '.join(deploys)})"
    leftovers = _cache_containers()
    if leftovers:
        return f"redis/cache docker container ({' '.join(leftovers)} )".replace(" )", ")")
    return None


def force_clean_cache_backend(env, say=lambda m: None):
    ns = env.get("NAMESPACE") or "ecb"
    for c in _cache_containers():
        say(f"force-clean: removing leftover docker container {c}")
        _run(["docker", "rm", "-f", c])
    # The pulumi sandbox is the agent's own docker daemon: anything it left
    # running there (a probe redis published on the sidecar's 6379) answers
    # on the cache mapping the guard pings. The verify owns the sandbox, so
    # clear it whole, as teardown.sh does.
    host = env.get("PULUMI_DOCKER_HOST")
    if host:
        rc, out, _ = _run(["docker", "-H", host, "ps", "-aq"])
        inner = out.split() if rc == 0 else []
        if inner:
            say(f"force-clean: removing {len(inner)} container(s) inside the "
                f"pulumi sandbox {host} ({' '.join(inner)})")
            _run(["docker", "-H", host, "rm", "-f", *inner])
    deploys = _k8s_cache_deployments(ns, env)
    if deploys:
        say(f"force-clean: deleting leftover k8s cache deploy/svc in ns/{ns} ({' '.join(deploys)})")
        _run(["kubectl", "delete", *[f"deploy/{d}" for d in deploys], "svc/redis",
              "-n", ns, "--ignore-not-found", "--wait=true"], env=env)


def clean_start_wait(env, say=lambda m: None):
    """True once no cache backend remains; polls ~30s, force-cleans once,
    False only when the rig is genuinely stuck."""
    tries = 0
    while assert_no_cache_backend(env) is not None:
        tries += 1
        if tries == 10:
            say("clean-start: residue persists after 30s — force-cleaning")
            force_clean_cache_backend(env, say)
        elif tries > 12:
            return False
        time.sleep(3)
    return True


# --- the fixed capped store --------------------------------------------------

_WARM_SQL = r"""\set n random(1, 1000000)
SELECT count(*) FROM (
  SELECT ts_rank(to_tsvector('english', md5(i::text || :n::text)), plainto_tsquery('english', :n::text)) AS r
  FROM generate_series(1, {ops}) AS s(i)
) ranked WHERE r >= 0;
"""


STORE_PREFIX = "fae-store-"
NET_PREFIX = "fae-net-"


def cell_identity(cid):
    """The store and the network it answers on, named per cell: nothing
    here is shared between cells, so no lock serializes verifies for the
    store's sake. The network is the cell's own (the engine's
    `fae-net-<cid>`, where the verify container already is), so the store
    is reached by name on the daemon's own port and publishes nothing on
    the host. The SERVICE port is not here: the task's docs name it (the
    KEDA manifests target `APP_HOST:8080/metrics`), and the verify lock
    already runs one service at a time."""
    return {"DB_CONTAINER": f"{STORE_PREFIX}{cid}", "DB_NET": f"{NET_PREFIX}{cid}",
            "DB_PORT": "5432"}


def _need(env, key):
    v = env.get(key)
    if not v:
        raise RuntimeError(f"store identity missing: {key} (cell_identity sets it)")
    return v


def _db(env):
    return {"image": env.get("DB_IMAGE") or "postgres:16.15",
            "container": _need(env, "DB_CONTAINER"),
            "port": _need(env, "DB_PORT"),
            "net": _need(env, "DB_NET"),
            "cpus": env.get("DB_CPUS") or "1",
            "cpuset": env.get("CPUSET_MEASURED") or os.environ.get("CPUSET_MEASURED") or "",
            "mem": env.get("DB_MEM") or "512m",
            "max_conn": env.get("DB_MAX_CONN") or "100",
            "name": env.get("DB_NAME") or "metadata"}


def db_up(env, say=lambda m: None):
    """Start the capped store, wait ready, warm it. Returns (ok, exports,
    err): exports carries the DSN and friends the next steps consume."""
    d = _db(env)
    if _run(["docker", "info"])[0] != 0:
        return False, {}, "docker required for the fixed store"
    if _run(["docker", "network", "inspect", d["net"]])[0] != 0:
        _run(["docker", "network", "create", d["net"]])
    _run(["docker", "rm", "-f", d["container"]])
    rc, _, err = _run([
        "docker", "run", "-d", "--name", d["container"], "--network", d["net"],
        f"--cpus={d['cpus']}", *([f"--cpuset-cpus={d['cpuset']}"] if d["cpuset"] else []),
        f"--memory={d['mem']}",
        "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=postgres",
        "-e", f"POSTGRES_DB={d['name']}",
        d["image"], "-c", f"max_connections={d['max_conn']}",
        "-c", "shared_buffers=128MB"])
    if rc != 0:
        return False, {}, f"could not start the fixed store container: {err.strip()}"
    # over TCP: the image's init server listens on the socket only, and a
    # socket probe passes against it just before it stops for the real start
    ready = ["docker", "exec", d["container"], "pg_isready", "-h", "127.0.0.1",
             "-U", "postgres", "-d", d["name"]]
    for _ in range(40):
        if _run(ready)[0] == 0:
            break
        time.sleep(1)
    if _run(ready)[0] != 0:
        return False, {}, "fixed store never became ready"
    exports = {"DB_CONTAINER": d["container"], "DB_PORT": d["port"],
               "DB_NET": d["net"], "DB_NAME": d["name"],
               "DB_HOST": d["container"], "DB_USER": "postgres",
               "DB_PASSWORD": "postgres",
               "DB_DSN": f"postgresql://postgres@{d['container']}:{d['port']}/{d['name']}",
               "QUERY_CPU_OPS": env.get("QUERY_CPU_OPS") or "5000"}
    db_warm(dict(env, **exports))
    return True, exports, ""


def db_down(env):
    """Remove the cell's store. The network is the cell's, the engine's to
    remove after the cell."""
    c = env.get("DB_CONTAINER")
    if c:
        _run(["docker", "rm", "-f", c])


def store_bench(env, tag):
    """The capped store's throughput on the daemon DOCKER_HOST names, on a
    store and network of its own named for `tag`, removed after; None when
    the bench did not run. `env` carries the store's image and caps."""
    env = dict(env, DB_CONTAINER=f"{STORE_PREFIX}{tag}", DB_NET=f"{NET_PREFIX}{tag}",
               DB_PORT="5432")
    try:
        ok, exports, err = db_up(env)
        if not ok:
            sys.stderr.write(f"store-bench: {err}\n")
            return None
        return db_bench_qps(dict(env, **exports), env.get("QUERY_CPU_OPS") or "5000")
    finally:
        db_down(env)
        _run(["docker", "network", "rm", env["DB_NET"]])


def db_warm(env):
    """Warm the store's CPU/page cache/connections from a client container so
    a cold start cannot trip the scaler at baseline. Best-effort."""
    d = _db(env)
    ops = env.get("QUERY_CPU_OPS") or "5000"
    secs = env.get("DB_WARM_SECS") or "8"
    sql = _WARM_SQL.format(ops=ops)
    try:
        _run(["docker", "run", "--rm", "-i", "--network", d["net"],
              *([f"--cpuset-cpus={d['cpuset']}"] if d["cpuset"] else []), d["image"],
              "sh", "-c",
              f"cat > /tmp/w.sql && pgbench -n -h '{d['container']}' -U postgres "
              f"-f /tmp/w.sql -c 4 -j 4 -T {secs} '{d['name']}'"],
             input_text=sql, timeout=int(secs) + 60)
    except subprocess.TimeoutExpired:
        pass


def db_assert_caps(env):
    """None when the live container carries the frozen cgroup caps, else the
    mismatch description (the verify voids on it)."""
    d = _db(env)
    _, nano, _ = _run(["docker", "inspect", "-f",
                       "{{.HostConfig.NanoCpus}}", d["container"]])
    _, mem, _ = _run(["docker", "inspect", "-f",
                      "{{.HostConfig.Memory}}", d["container"]])
    want_nano = str(int(float(d["cpus"]) * 1_000_000_000))
    want_mem = str(mem_bytes(d["mem"]))
    if nano.strip() != want_nano or mem.strip() != want_mem:
        return (f"store caps mismatch: nanocpus={nano.strip()}/want={want_nano} "
                f"mem={mem.strip()}/want={want_mem}")
    return None


def db_bench_qps(env, ops):
    """Sustained tps at pool concurrency, measured from a client container.
    None when the bench did not run or reported no tps: a missing store is
    not a slow one."""
    d = _db(env)
    clients = env.get("DB_BENCH_CLIENTS") or "8"
    secs = env.get("DB_BENCH_SECS") or "10"
    sql = _WARM_SQL.format(ops=ops)
    rc, out, _ = _run(["docker", "run", "--rm", "-i", "--network", d["net"],
                       *([f"--cpuset-cpus={d['cpuset']}"] if d["cpuset"] else []),
                       d["image"], "sh", "-c",
                       f"cat > /tmp/cal.sql && pgbench -n -h '{d['container']}' "
                       f"-U postgres -f /tmp/cal.sql -c {clients} -j {clients} "
                       f"-T {secs} '{d['name']}'"],
                      input_text=sql, timeout=int(secs) + 120)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("tps"):
            try:
                return int(float(line.split()[2]))
            except (IndexError, ValueError):
                return None
    return None


# --- cache-backend ground truth (the sidecar's mount probe) ------------------

def cache_backend_ready(env):
    """True iff a cache backend is running and reachable RIGHT NOW — a redis
    PING through a sidecar mapping, a k8s redis endpoint that is ready, or a
    docker redis container answering PING. The sidecar polls this ~1 Hz and
    gates the mount timestamp on it, so the measured mount reflects when the
    cache is provably serving, not when /health first claims it."""
    ph, pp = env.get("PULUMI_CACHE_HOST"), env.get("PULUMI_CACHE_PORT")
    if ph:
        return bool(pp and _redis_ping(ph, pp))
    ns = env.get("NAMESPACE") or "ecb"
    if shutil.which("kubectl") and _run(["kubectl", "get", "svc", "redis", "-n", ns], env=env)[0] == 0:
        rc, out, _ = _run(
            ["kubectl", "get", "endpointslices", "-n", ns, "-l",
             "kubernetes.io/service-name=redis", "-o",
             'jsonpath={range .items[*].endpoints[?(@.conditions.ready==true)]}'
             '{.addresses[0]}{" "}{end}'], env=env)
        return rc == 0 and bool(out.strip())
    for c in _cache_containers():
        rc, out, _ = _run(["docker", "exec", c, "redis-cli", "ping"])
        if rc == 0 and "pong" in out.lower():
            return True
    return False


if __name__ == "__main__":
    if sys.argv[1:2] == ["cache-ready"]:
        sys.exit(0 if cache_backend_ready(os.environ) else 1)
    if sys.argv[1:2] == ["store-bench"] and len(sys.argv) == 4:
        qps = store_bench(json.loads(sys.argv[3]), sys.argv[2])
        print(json.dumps({"store_qps": qps}))
        sys.exit(0 if qps is not None else 1)
    sys.stderr.write("usage: python3 -m experiment.verifier.rig cache-ready\n"
                     "       python3 rig.py store-bench TAG STORE_ENV_JSON\n")
    sys.exit(2)
