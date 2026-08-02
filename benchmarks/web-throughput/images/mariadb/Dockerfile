FROM mariadb@sha256:09336a8c0cff9f363e133d8a245cca5ad3eeb326e0ffaedf74d49214c4571486

ARG CREATED
ARG REVISION
ARG SOURCE
ARG VERSION

LABEL org.opencontainers.image.title="MariaDB RunPod benchmark wrapper" \
      org.opencontainers.image.description="Exact MariaDB application layers with SSH bootstrap and fail-closed TLS/snapshot startup" \
      org.opencontainers.image.licenses="GPL-2.0" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="docker.io/library/mariadb:11.8.8" \
      org.opencontainers.image.base.digest="sha256:09336a8c0cff9f363e133d8a245cca5ad3eeb326e0ffaedf74d49214c4571486" \
      io.bluemap.benchmark.mariadb-version="11.8.8" \
      io.bluemap.benchmark.wrapper.packages="openssh-server,iproute2,procps,zstd"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
        iproute2 \
        openssh-server \
        openssl \
        procps \
        zstd \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o root -g root -m 0755 \
        /bootstrap \
        /docker-entrypoint-initdb.d \
        /run/sshd \
    && rm -f /etc/ssh/ssh_host_*

COPY benchmarks/web-throughput/images/common/bootstrap.sh /usr/local/libexec/bluemap-bootstrap.sh
COPY benchmarks/web-throughput/images/common/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 benchmarks/web-throughput/images/mariadb/entrypoint.sh /usr/local/bin/bluemap-mariadb-entrypoint

EXPOSE 22 3306

ENTRYPOINT ["/usr/local/bin/bluemap-mariadb-entrypoint"]
