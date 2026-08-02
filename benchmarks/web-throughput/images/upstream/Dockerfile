FROM ghcr.io/bluemap-minecraft/bluemap@sha256:5760d82b0cc3dbc1ef2482a45a8d283e680687e60c57aa17dd9fa083184dc062

ARG CREATED
ARG REVISION
ARG SOURCE
ARG VERSION

LABEL org.opencontainers.image.title="BlueMap upstream RunPod benchmark wrapper" \
      org.opencontainers.image.description="Exact upstream BlueMap CLI application layers with isolated SSH bootstrap and readiness gating" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="ghcr.io/bluemap-minecraft/bluemap" \
      org.opencontainers.image.base.digest="sha256:5760d82b0cc3dbc1ef2482a45a8d283e680687e60c57aa17dd9fa083184dc062" \
      io.bluemap.benchmark.application-revision="e664c1abdf697c64703401dca1d7e1956f755f65" \
      io.bluemap.benchmark.wrapper.packages="openssh-server,curl,iproute2,iptables,procps,util-linux"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        iproute2 \
        iptables \
        openssh-server \
        procps \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bluemap \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app bluemap \
    && install -d -o root -g root -m 0755 /bootstrap /run/sshd \
    && install -d -o bluemap -g bluemap -m 0755 /app/data /app/web /data /web \
    && rm -f /etc/ssh/ssh_host_*

COPY benchmarks/web-throughput/images/common/bootstrap.sh /usr/local/libexec/bluemap-bootstrap.sh
COPY benchmarks/web-throughput/images/common/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 benchmarks/web-throughput/images/upstream/entrypoint.sh /usr/local/bin/bluemap-upstream-entrypoint

EXPOSE 22 8100

ENTRYPOINT ["/usr/local/bin/bluemap-upstream-entrypoint"]
