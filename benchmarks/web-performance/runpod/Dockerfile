FROM grafana/k6:2.1.0@sha256:68e78d94140704ec4ee0cb7c5cf6cd12a32b7d310a6f98d94931ee9b0b9dc629 AS k6

FROM alpine:3.23@sha256:1beb0dc0a51de7ff38e3b5274078a2e0b81113ba5c7535e1a03d5913a5edbda3

RUN apk add --no-cache \
        bash \
        ca-certificates \
        coreutils \
        curl \
        jq \
        openssh-server \
    && addgroup -g 12345 loadgen \
    && adduser -D -h /home/loadgen -u 12345 -G loadgen loadgen \
    && passwd -d loadgen \
    && install -d -m 0700 -o loadgen -g loadgen /home/loadgen/.ssh \
    && install -d -m 0750 -o loadgen -g loadgen /artifacts \
    && install -d -m 0755 -o root -g root /runner \
    && install -d -m 0755 /run/sshd

COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6
COPY benchmarks/web-performance/runpod/entrypoint.sh /usr/local/bin/bluemap-runpod-entrypoint
COPY benchmarks/web-performance/runpod/identity.sh /usr/local/bin/bluemap-runpod-identity
COPY benchmarks/web-performance/runpod/run-phase.sh /usr/local/bin/bluemap-runpod-run-phase
COPY benchmarks/web-performance/runpod/sample-resources.sh /usr/local/bin/bluemap-runpod-sample-resources

RUN chmod 0755 \
        /usr/local/bin/bluemap-runpod-entrypoint \
        /usr/local/bin/bluemap-runpod-identity \
        /usr/local/bin/bluemap-runpod-run-phase \
        /usr/local/bin/bluemap-runpod-sample-resources \
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
        'AllowTcpForwarding no' \
        'GatewayPorts no' \
        'X11Forwarding no' \
        'PermitTunnel no' \
        'PermitUserEnvironment no' \
        'ClientAliveInterval 30' \
        'ClientAliveCountMax 3' \
        'Subsystem sftp internal-sftp' \
        > /etc/ssh/sshd_config

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/bluemap-runpod-entrypoint"]
