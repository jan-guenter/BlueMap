FROM grafana/k6:2.1.0@sha256:68e78d94140704ec4ee0cb7c5cf6cd12a32b7d310a6f98d94931ee9b0b9dc629 AS k6

FROM alpine:3.23@sha256:1beb0dc0a51de7ff38e3b5274078a2e0b81113ba5c7535e1a03d5913a5edbda3

ARG BLUEMAP_SOURCE_REVISION

RUN apk add --no-cache \
        bash \
        ca-certificates \
        coreutils \
        curl \
        haproxy=3.2.22-r0 \
        jq \
        openssh-server \
        socat=1.8.1.3-r0 \
        tini \
        util-linux \
    && addgroup -g 12345 loadgen \
    && adduser -D -h /home/loadgen -u 12345 -G loadgen loadgen \
    && passwd -d loadgen \
    && install -d -m 0700 -o loadgen -g loadgen /home/loadgen/.ssh \
    && install -d -m 0750 -o loadgen -g loadgen /artifacts \
    && install -d -m 0755 -o root -g root /runner \
    && install -d -m 0755 /run/sshd \
    && install -d -m 0755 -o root -g root /run/haproxy

RUN [ "${#BLUEMAP_SOURCE_REVISION}" -eq 40 ] \
    && case "$BLUEMAP_SOURCE_REVISION" in \
        *[!a-f0-9]*) exit 1 ;; \
    esac \
    && [ "$BLUEMAP_SOURCE_REVISION" != "0000000000000000000000000000000000000000" ] \
    && jq -n \
        --arg sourceRevision "$BLUEMAP_SOURCE_REVISION" \
        '{formatVersion: 1, sourceRevision: $sourceRevision}' \
        > /runner/loadgen-build.json \
    && chown root:root /runner/loadgen-build.json \
    && chmod 0444 /runner/loadgen-build.json

COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6
COPY benchmarks/web-performance/runpod/haproxy.cfg /etc/haproxy/haproxy.cfg
COPY benchmarks/web-performance/runpod/entrypoint.sh /usr/local/bin/bluemap-runpod-entrypoint
COPY benchmarks/web-performance/runpod/identity.sh /usr/local/bin/bluemap-runpod-identity
COPY benchmarks/web-performance/runpod/run-phase.sh /usr/local/bin/bluemap-runpod-run-phase
COPY benchmarks/web-performance/runpod/sample-resources.sh /usr/local/bin/bluemap-runpod-sample-resources

RUN chmod 0755 \
        /usr/local/bin/bluemap-runpod-entrypoint \
        /usr/local/bin/bluemap-runpod-identity \
        /usr/local/bin/bluemap-runpod-run-phase \
        /usr/local/bin/bluemap-runpod-sample-resources \
    && chown root:root /etc/haproxy/haproxy.cfg \
    && chmod 0444 /etc/haproxy/haproxy.cfg \
    && test -x /sbin/tini \
    && command -v setsid >/dev/null \
    && command -v socat >/dev/null \
    && haproxy -c -f /etc/haproxy/haproxy.cfg \
    && printf '%s\n' \
        'Port 22' \
        'AddressFamily any' \
        'HostKey /etc/ssh/ssh_host_ed25519_key' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'PermitRootLogin no' \
        'PermitEmptyPasswords no' \
        'PubkeyAuthentication yes' \
        'AuthorizedKeysFile .ssh/authorized_keys' \
        'AllowUsers loadgen' \
        'AllowAgentForwarding no' \
        'AllowTcpForwarding remote' \
        'AllowStreamLocalForwarding no' \
        'GatewayPorts no' \
        'PermitListen 127.0.0.1:18081 127.0.0.1:18082 127.0.0.1:18083 127.0.0.1:18084 127.0.0.1:18085 127.0.0.1:18086 127.0.0.1:18087 127.0.0.1:18088' \
        'X11Forwarding no' \
        'PermitTunnel no' \
        'PermitUserEnvironment no' \
        'ClientAliveInterval 30' \
        'ClientAliveCountMax 3' \
        'Subsystem sftp internal-sftp' \
        > /etc/ssh/sshd_config

EXPOSE 22

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/bluemap-runpod-entrypoint"]
