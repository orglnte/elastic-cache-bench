# The experiment's verify environment: the engine's runtime, the docker
# client for what an arrangement provisions over the daemon's socket, k6 for
# the load, and the judged service's own runtime — it runs here, as a child
# of the verifier, over the cell's artifacts. Each variant layers its tools
# on this image (variants/<tech>/verify/Dockerfile, FROM $BASE).
FROM python:3.12.13-slim
ARG DOCKER_CLI_VERSION=27.1.1
ARG K6_VERSION=2.1.0
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl lsof procps \
    && arch="$(dpkg --print-architecture)"; case "$arch" in \
        amd64) dc=x86_64 ;; arm64) dc=aarch64 ;; *) dc="$arch" ;; esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dc}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && curl -fsSL "https://github.com/grafana/k6/releases/download/v${K6_VERSION}/k6-v${K6_VERSION}-linux-${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "k6-v${K6_VERSION}-linux-${arch}/k6" \
    && docker --version && k6 version \
    && rm -rf /var/lib/apt/lists/*
# the service's runtime: what task/skeleton/requirements.txt asks for, pinned
RUN pip install \
        fastapi==0.139.1 "uvicorn[standard]==0.51.0" "psycopg[binary,pool]==3.3.4" \
        redis==8.0.1 httpx==0.28.1 pydantic==2.13.4
