# The KEDA arms' agent layer over the agents' base: what a KEDA agent may use
# while authoring, and only that.
#   * kubeconform  — offline manifest schema lint
#   * kubectl      — keda_access arms apply manifests to their own sandbox
#                    cluster (inert for the sealed arms: no kubeconfig)
#   * k6, pytest, psycopg, redis — the tools every arm's layer carries
# Versions match the verify image's (variants/keda/verify/Dockerfile).
ARG BASE
FROM $BASE
USER root
ARG KUBECONFORM_VERSION=0.6.7
ARG KUBECTL_VERSION=1.36.1
RUN arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-linux-${arch}.tar.gz" \
      | tar -xz -C /usr/local/bin kubeconform \
    && curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
      -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl \
    && kubeconform -v && kubectl version --client
ARG K6_VERSION=2.1.0
RUN arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/grafana/k6/releases/download/v${K6_VERSION}/k6-v${K6_VERSION}-linux-${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "k6-v${K6_VERSION}-linux-${arch}/k6" \
    && k6 version
RUN python3 -m pip install --break-system-packages --no-cache-dir "psycopg[binary]" redis pytest
USER node
