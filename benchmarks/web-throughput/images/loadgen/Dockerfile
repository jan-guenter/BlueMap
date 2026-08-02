FROM grafana/k6:1.3.0@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d AS k6

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

ARG CREATED
ARG REVISION
ARG SOURCE
ARG VERSION

LABEL org.opencontainers.image.title="BlueMap RunPod throughput controller" \
      org.opencontainers.image.description="Pinned k6 and control tooling for the isolated RunPod MariaDB comparison" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.13-slim-bookworm" \
      org.opencontainers.image.base.digest="sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64" \
      io.bluemap.benchmark.k6-base.digest="sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d" \
      io.bluemap.benchmark.wrapper.packages="openssh-server,openssh-client,haproxy,mariadb-client,age,rclone"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        age \
        bash \
        ca-certificates \
        curl \
        haproxy \
        iproute2 \
        jq \
        mariadb-client \
        netcat-openbsd \
        openssh-client \
        openssh-server \
        openssl \
        procps \
        rclone \
        socat \
        tini \
        zstd \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o root -g root -m 0755 /artifacts /bootstrap /opt/bluemap/benchmark /run/sshd \
    && rm -f /etc/ssh/ssh_host_*

COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6
COPY benchmarks/web-throughput/ /opt/bluemap/benchmark/
COPY benchmarks/web-throughput/images/common/bootstrap.sh /usr/local/libexec/bluemap-bootstrap.sh
COPY benchmarks/web-throughput/images/common/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 benchmarks/web-throughput/images/loadgen/entrypoint.sh /usr/local/bin/bluemap-loadgen-entrypoint

WORKDIR /opt/bluemap/benchmark

EXPOSE 22

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/bluemap-loadgen-entrypoint"]
CMD ["sleep", "infinity"]
