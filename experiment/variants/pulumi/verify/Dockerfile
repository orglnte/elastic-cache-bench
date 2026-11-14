# The pulumi variants' tools over the verifier's image: the Pulumi CLI, its
# Python SDK and the docker provider plugin, pinned — the service under test
# provisions its cache through them at runtime, against the cell's sidecar.
ARG BASE
FROM $BASE
ARG PULUMI_VERSION=3.251.0
ARG PULUMI_PY_VERSION=3.253.0
ARG PULUMI_DOCKER_VERSION=4.11.2
ENV PULUMI_HOME=/opt/pulumi-home PULUMI_SKIP_UPDATE_CHECK=true PATH=/opt/pulumi:$PATH
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-${arch}.tar.gz" \
       | tar -xz -C /opt \
    && pip install pulumi==${PULUMI_PY_VERSION} pulumi_docker==${PULUMI_DOCKER_VERSION} \
    && pulumi plugin install resource docker v${PULUMI_DOCKER_VERSION} \
    && chmod -R a+rwX ${PULUMI_HOME} \
    && pulumi version && python3 -c "import pulumi, pulumi_docker"
