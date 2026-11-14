# The keda variants' tools over the verifier's image: kind provisions the
# per-verify cluster over the daemon's socket, kubectl applies the authored
# manifests and port-forwards the cache to the service.
ARG BASE
FROM $BASE
ARG KIND_VERSION=0.32.0
ARG KUBECTL_VERSION=1.36.1
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${arch}" \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
    && chmod +x /usr/local/bin/kind /usr/local/bin/kubectl \
    && kind version && kubectl version --client
