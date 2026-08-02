FROM eclipse-temurin:25-jre-jammy@sha256:b8ba5fca9d88b6ecc3a46c8e75b744f84aca9a9d08587901b5ab480baf641ab5

ARG CREATED
ARG REVISION
ARG SOURCE
ARG VERSION

LABEL org.opencontainers.image.title="BlueMap Java RunPod benchmark wrapper" \
      org.opencontainers.image.description="Stateless standalone BlueMap Java map-data server with isolated SSH bootstrap and readiness gating" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="docker.io/library/eclipse-temurin:25-jre-jammy" \
      org.opencontainers.image.base.digest="sha256:b8ba5fca9d88b6ecc3a46c8e75b744f84aca9a9d08587901b5ab480baf641ab5" \
      io.bluemap.benchmark.wrapper.packages="openssh-server,curl,iproute2,procps,util-linux"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        iproute2 \
        openssh-server \
        procps \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bluemap \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /opt/bluemap bluemap \
    && install -d -o root -g root -m 0755 /bootstrap /run/sshd \
    && install -d -o bluemap -g bluemap -m 0755 /opt/bluemap /data /tmp \
    && rm -f /etc/ssh/ssh_host_*

WORKDIR /opt/bluemap

COPY --chown=10001:10001 build/release/*-webserver.jar /opt/bluemap/bluemap-web.jar
COPY benchmarks/web-throughput/images/common/bootstrap.sh /usr/local/libexec/bluemap-bootstrap.sh
COPY benchmarks/web-throughput/images/common/sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 benchmarks/web-throughput/images/java/entrypoint.sh /usr/local/bin/bluemap-java-entrypoint

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/bluemap-java-entrypoint"]
