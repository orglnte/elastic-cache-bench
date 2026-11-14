# The Pulumi arms' agent layer over the agents' base: what a Pulumi agent may
# use while authoring, and only that.
#   * docker CLI   — pulumi_access drives its own sidecar daemon through the
#                    DOCKER_HOST the driver injects (inert otherwise)
#   * k6, pytest, psycopg, redis — the tools every arm's layer carries
ARG BASE
FROM $BASE
USER root
ARG DOCKER_CLI_VERSION=27.1.1
RUN arch="$(dpkg --print-architecture)"; case "$arch" in \
        amd64) dc=x86_64 ;; arm64) dc=aarch64 ;; *) dc="$arch" ;; esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dc}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version
ARG K6_VERSION=2.1.0
RUN arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/grafana/k6/releases/download/v${K6_VERSION}/k6-v${K6_VERSION}-linux-${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "k6-v${K6_VERSION}-linux-${arch}/k6" \
    && k6 version
RUN python3 -m pip install --break-system-packages --no-cache-dir "psycopg[binary]" redis pytest
USER node
